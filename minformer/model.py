"""Minimal model definition."""

import os

os.environ["XLA_FLAGS"] = "--xla_force_host_platform_device_count=8"


import functools
import math
import jax
import jax.numpy as jnp
from flax import struct, nnx
from jax.sharding import PartitionSpec as P
from jax.experimental.shard_map import shard_map
from jax.experimental.pallas.ops.tpu import flash_attention
import dataclasses
import orbax.checkpoint as ocp
from typing import Any, NamedTuple


def create_mesh():
  """Always 1D because only care about FSDP."""
  devices = jax.devices()
  mesh = jax.sharding.Mesh(devices, ("x",))
  return mesh


class ShardingRules(NamedTuple):
  batch: str | None
  sequence: str | None
  d_model: str | None
  query_heads: str | None
  key_heads: str | None
  key_dim: str | None
  ffw: str | None
  vocab: str | None

  def __call__(self, *keys: str) -> tuple[str, ...]:
    return tuple(getattr(self, key) for key in keys)


fsdp_rules = ShardingRules(
  batch="x",
  sequence=None,
  d_model="x",
  query_heads=None,
  key_heads=None,
  key_dim=None,
  ffw=None,
  vocab=None,
)

mdl_parallel_rules = ShardingRules(
  batch=None,
  sequence=None,
  d_model=None,
  query_heads="x",
  key_heads="x",
  key_dim=None,
  ffw="x",
  vocab=None,
)


@dataclasses.dataclass
class Config:
  d_model: int
  ffw_multiplier: int
  query_heads: int
  key_heads: int
  num_layers: int
  key_dim: int
  vocab_size: int
  # Max seq len here can be a source of nasty bugs in incremental prefill
  # if we overflow (since dynamic slice will shunt left instead of erroring. Fix?
  max_seq_len: int
  causal: bool
  use_attn_kernel: bool
  weight_dtype: jnp.float32
  # Sharding rules
  rules: ShardingRules
  mesh: jax.sharding.Mesh | None
  # Optimizer config
  max_lr: float = 3e-4
  min_lr: float = 1e-5
  warmup_steps: int = 50
  total_steps: int = 10000


class Layer(nnx.Module):
  def __init__(self, cfg: Config, rngs: nnx.Rngs):
    self.q = nnx.Param(
      nnx.initializers.he_normal(in_axis=0, out_axis=(1, 2))(
        rngs.params(),
        (cfg.d_model, cfg.query_heads, cfg.key_dim),
        dtype=cfg.weight_dtype,
      ),
      sharding=cfg.rules("d_model", "query_heads", "key_dim"),
    )
    self.k = nnx.Param(
      nnx.initializers.he_normal(in_axis=0, out_axis=(1, 2))(
        rngs.params(),
        (cfg.d_model, cfg.key_heads, cfg.key_dim),
        dtype=cfg.weight_dtype,
      ),
      sharding=cfg.rules("d_model", "key_heads", "key_dim"),
    )
    self.v = nnx.Param(
      nnx.initializers.he_normal(in_axis=0, out_axis=(1, 2))(
        rngs.params(),
        (cfg.d_model, cfg.key_heads, cfg.key_dim),
        dtype=cfg.weight_dtype,
      ),
      sharding=cfg.rules("d_model", "key_heads", "key_dim"),
    )
    self.proj = nnx.Param(
      nnx.initializers.he_normal(in_axis=(0, 1), out_axis=2)(
        rngs.params(),
        (cfg.query_heads, cfg.key_dim, cfg.d_model),
        dtype=cfg.weight_dtype,
      ),
      sharding=cfg.rules("query_heads", "key_dim", "d_model"),
    )
    self.w1 = nnx.Param(
      nnx.initializers.he_normal(in_axis=0, out_axis=1)(
        rngs.params(),
        (cfg.d_model, cfg.d_model * cfg.ffw_multiplier),
        dtype=cfg.weight_dtype,
      ),
      sharding=cfg.rules("d_model", "ffw"),
    )
    self.w2 = nnx.Param(
      nnx.initializers.he_normal(in_axis=1, out_axis=0)(
        rngs.params(),
        (cfg.d_model * cfg.ffw_multiplier, cfg.d_model),
        dtype=cfg.weight_dtype,
      ),
      sharding=cfg.rules("ffw", "d_model"),
    )
    self.gamma1 = nnx.Param(
      jnp.ones((cfg.d_model,), dtype=cfg.weight_dtype),
      sharding=cfg.rules("d_model"),
    )
    self.gamma2 = nnx.Param(
      jnp.ones((cfg.d_model,), dtype=cfg.weight_dtype),
      sharding=cfg.rules("d_model"),
    )


class Weights(nnx.Module, experimental_pytree=True):
  def __init__(self, cfg: Config, rngs: nnx.Rngs):
    self.layers = [Layer(cfg, rngs) for _ in range(cfg.num_layers)]
    self.embedding = nnx.Param(
      jax.nn.initializers.he_normal(in_axis=0, out_axis=1)(
        rngs.params(), (cfg.vocab_size, cfg.d_model), dtype=cfg.weight_dtype
      ),
      sharding=cfg.rules("vocab", "d_model"),
    )
    self.vocab_proj = nnx.Param(
      jax.nn.initializers.he_normal(in_axis=0, out_axis=1)(
        rngs.params(), (cfg.d_model, cfg.vocab_size), dtype=cfg.weight_dtype
      ),
      sharding=cfg.rules("d_model", "vocab"),
    )

  @classmethod
  def init(
    cls,
    cfg: Config,
    rngs: nnx.Rngs,
    use_low_mem_init: bool = False,
  ) -> "Weights":
    # This takes ~10-20s instead of 0.1s to init, but will init sharded. Use this when the
    # weights would be too big for one device.
    # Ideal version with Pytree Modules:
    def init_fn():
      return cls(cfg, nnx.clone(rngs))

    assert cfg.mesh is not None
    shardings = nnx.get_named_sharding(jax.eval_shape(init_fn), cfg.mesh)
    if use_low_mem_init:
      return jax.jit(init_fn, out_shardings=shardings)()
    return jax.device_put(cls(cfg, rngs), shardings)

    # Current state version we use:
    # def create_fn(rngs):
    #   weights = cls(cfg, rngs)
    #   state = nnx.state(weights)
    #   shardings = nnx.get_named_sharding(state, mesh)
    #   if use_low_mem_init:
    #     state = jax.lax.with_sharding_constraint(state, shardings)
    #   else:
    #     state = jax.device_put(state, shardings)
    #   nnx.update(weights, state)
    #   return weights

    # if use_low_mem_init:
    #   return jax.jit(create_fn)(rngs)
    # return create_fn(rngs)


test_cfg = Config(
  d_model=4 * 64,
  ffw_multiplier=4,
  query_heads=4,
  key_heads=4,
  num_layers=1,
  key_dim=64,
  vocab_size=1000,
  max_seq_len=1024,
  causal=True,
  use_attn_kernel=True,
  weight_dtype=jnp.float32,
  rules=ShardingRules(
    batch="x",
    sequence=None,
    d_model="x",
    query_heads=None,
    key_heads=None,
    key_dim=None,
    ffw=None,
    vocab=None,
  ),
  mesh=create_mesh(),
)
model = Weights.init(test_cfg, nnx.Rngs(0), use_low_mem_init=True)
print(model)

exit()


@struct.dataclass
class KVCache:
  k: list[jax.Array]  # (batch_size, key_heads, max_seq_len, key_dim)
  v: list[jax.Array]  # (batch_size, key_heads, max_seq_len, key_dim)
  lengths: jax.Array  # [batch_size]

  @classmethod
  def shape(cls, cfg: Config, batch_size: int, max_seq_len: int) -> "KVCache":
    return KVCache(
      k=[
        jax.ShapeDtypeStruct(
          (batch_size, cfg.key_heads, max_seq_len, cfg.key_dim), jnp.bfloat16
        )
        for _ in range(cfg.num_layers)
      ],
      v=[
        jax.ShapeDtypeStruct(
          (batch_size, cfg.key_heads, max_seq_len, cfg.key_dim), jnp.bfloat16
        )
        for _ in range(cfg.num_layers)
      ],
      lengths=jax.ShapeDtypeStruct((batch_size,), jnp.int32),
    )

  @classmethod
  def logical_axes(cls, cfg: Config) -> "KVCache":
    del cfg
    return KVCache(
      k=[P("batch", "key_heads", "sequence", "key_dim") for _ in range(cfg.num_layers)],
      v=[P("batch", "key_heads", "sequence", "key_dim") for _ in range(cfg.num_layers)],
      lengths=P("batch"),
    )

  @classmethod
  def shardings(
    cls, cfg: Config, mesh: jax.sharding.Mesh, rules: ShardingRules
  ) -> "KVCache":
    return KVCache(
      k=[
        _logical_to_sharding(logical, mesh, rules)
        for logical in cls.logical_axes(cfg).k
      ],
      v=[
        _logical_to_sharding(logical, mesh, rules)
        for logical in cls.logical_axes(cfg).v
      ],
      lengths=_logical_to_sharding(cls.logical_axes(cfg).lengths, mesh, rules),
    )

  @classmethod
  def init(cls, cfg: Config, batch_size: int, max_seq_len: int) -> "KVCache":
    shape = cls.shape(cfg, batch_size, max_seq_len)
    return KVCache(
      k=[jnp.zeros(layer_shape.shape, layer_shape.dtype) for layer_shape in shape.k],
      v=[jnp.zeros(layer_shape.shape, layer_shape.dtype) for layer_shape in shape.v],
      lengths=jnp.zeros(shape.lengths.shape, shape.lengths.dtype),
    )

  @property
  def time_axis(cls) -> int:
    return 2


def _generate_fixed_pos_embedding(
  features, length, min_timescale=1.0, max_timescale=10000.0
) -> tuple[jax.Array, jax.Array]:
  """Generate Sin/Cos for Rotary Embeddings.

  Generates sinusoids at (features//2) different timescales, where the
  timescales form a geometric series from min_timescale to max_timescale
  (max_timescale is not included, but would be the next element in the series).

  Sinusoids are evaluated at integer positions i in [0, length).

  The outputs are computed as:

    output_sin[i, j] = sin(i / timescale[j])
    output_cos[i, j] = cos(i / timescale[j])

  Args:
    features: an integer
    length: an integer
    min_timescale: an optional float
    max_timescale: an optional float

  Returns:
    output_sin: a float32 Tensor with shape [length, features // 2]
    output_cos: a float32 Tensor with shape [length, features // 2]
  """
  # Forked from
  # flaxformer/components/embedding.py;l=592
  fraction = jnp.arange(0, features, 2, dtype=jnp.float32) / features
  timescale = min_timescale * (max_timescale / min_timescale) ** fraction
  rotational_frequency = 1.0 / timescale
  # Must use high precision einsum here, since rounding off to a bfloat16 is
  # catastrophic. bfloat16 rounds 257 to 256, but sin(257) is very different
  # from sin(256).
  sinusoid_inp = jnp.einsum(
    "i , j -> i j",
    jnp.arange(length),
    rotational_frequency,
    precision=jax.lax.Precision.HIGHEST,
  )
  return jnp.sin(sinusoid_inp), jnp.cos(sinusoid_inp)


def apply_rotary_embedding(x, sin, cos):
  assert x.ndim == 4
  assert sin.ndim == 3 and cos.ndim == 3
  x1, x2 = jnp.split(x, 2, axis=-1)
  sin, cos = (
    sin[:, None, :, :],
    cos[:, None, :, :],
  )  # [B, T, head_dim] -> [B, h, T, head_dim]
  return jnp.concatenate([x1 * cos - x2 * sin, x2 * cos + x1 * sin], axis=-1)


# Helper functions for RoPE lookups
def slice_at(table, index, length):
  return jax.lax.dynamic_slice_in_dim(table, index, length)


def slices_at(table, indices, length: int):
  return jax.vmap(functools.partial(slice_at, length=length), in_axes=(None, 0))(
    table, indices
  )


def make_attention_mask(
  q_len, k_len, q_segment_ids, k_segment_ids, q_offset, causal: bool
):
  # [B, t, T]
  segment_mask = q_segment_ids[:, :, None] == k_segment_ids[:, None, :]
  # [B, t, T] -> [B, 1, t, T]
  segment_mask = segment_mask[:, None, :, :]

  if causal:
    # [b, h, t, T]
    qk = (1, 1, q_len, k_len)
    q_iota = jax.lax.broadcasted_iota(jnp.int32, qk, 2)
    k_iota = jax.lax.broadcasted_iota(jnp.int32, qk, 3)
    q_positions = q_iota + q_offset[:, None, None, None]
    causal_mask = q_positions >= k_iota
    combined_mask = jnp.logical_and(segment_mask, causal_mask)
    return combined_mask
  else:
    return segment_mask


def attention(
  q: jax.Array,
  k: jax.Array,
  v: jax.Array,
  q_segment_ids: jax.Array,
  k_segment_ids: jax.Array,
  q_offset: jax.Array,
  cfg: Config,
) -> jax.Array:
  """
  Compute attention.

  Args:
  q: Query tensor of shape (batch_size, num_heads, q_len, head_dim)
  k: Key tensor of shape (batch_size, num_heads, k_len, head_dim)
  v: Value tensor of shape (batch_size, num_heads, k_len, head_dim)
  q_segment_ids: Query segment IDs of shape (batch_size, q_len)
  k_segment_ids: Key segment IDs of shape (batch_size, k_len)
  q_offset: Query offset of shape (batch_size,)
  cfg: Configuration object

  Returns:
  Attention output of shape (batch_size, num_heads, q_len, head_dim)
  """
  # Div sqrt(key_dim)
  scale = q.shape[-1] ** -0.5
  qk = jnp.einsum("bhtd,bhTd->bhtT", q, k) * scale
  mask = make_attention_mask(
    q.shape[2], k.shape[2], q_segment_ids, k_segment_ids, q_offset, cfg.causal
  )
  # Apply the combined mask

  max_score = jnp.max(qk, axis=-1, keepdims=True)
  qk = jnp.where(mask, qk, -1e30)
  # Jax softmax impl includes max subtraction for numerical stability, no need to
  # do it outside.
  attn = jax.nn.softmax(qk.astype(jnp.float32), axis=-1)
  return jnp.einsum("bhtT,bhTd->bhtd", attn, v).astype(jnp.bfloat16)


def attention_kernel(q, k, v, q_segment_ids, kv_segment_ids, cfg: Config):
  """Flash attention kernel!"""

  # On TPUv3, pallas seems to only work with float32.
  q, k, v = jnp.float32(q), jnp.float32(k), jnp.float32(v)
  scale = q.shape[-1] ** -0.5

  @functools.partial(
    shard_map,
    mesh=cfg.mesh,
    in_specs=(
      _logical_to_physical(P("batch", "query_heads", "sequence", "key_dim"), cfg.rules),
      _logical_to_physical(P("batch", "key_heads", "sequence", "key_dim"), cfg.rules),
      _logical_to_physical(P("batch", "key_heads", "sequence", "key_dim"), cfg.rules),
      _logical_to_physical(P("batch", "sequence"), cfg.rules),
      _logical_to_physical(P("batch", "sequence"), cfg.rules),
    ),
    out_specs=_logical_to_physical(
      P("batch", "query_heads", "sequence", "key_dim"), cfg.rules
    ),
    check_rep=False,
  )
  def _f(q, k, v, q_segment_ids, kv_segment_ids):
    segment_ids = flash_attention.SegmentIds(q_segment_ids, kv_segment_ids)
    return flash_attention.flash_attention(
      q, k, v, segment_ids=segment_ids, causal=True, sm_scale=scale
    )

  return _f(q, k, v, q_segment_ids, kv_segment_ids).astype(jnp.bfloat16)


def rms_norm(x: jax.Array, gamma: jax.Array) -> jax.Array:
  """Apply RMS normalization."""
  rms = jnp.sqrt(jnp.mean(x**2, axis=-1, keepdims=True) + 1e-6)
  return gamma * x / rms


def forward_layer(
  x: jax.Array,
  segment_ids: jax.Array,
  layer: Layer,
  sin: jax.Array,
  cos: jax.Array,
  idx: int,
  cfg: Config,
  cache: KVCache | None = None,
) -> tuple[jax.Array, jax.Array, jax.Array]:
  # First RMSNorm (Pre-LN for attention)

  # Cast layer to bfloat16 for faster operations.
  # layer = jax.tree.map(lambda x: jnp.bfloat16(x), layer)
  with jax.named_scope("attn_pre_norm"):
    attn_in = rms_norm(x, layer.gamma1)

  # Multi-head attention
  with jax.named_scope("qkv_matmul"):
    q = jnp.einsum("btd,dhq->bhtq", attn_in, layer.q)
    k = jnp.einsum("btd,dhk->bhtk", attn_in, layer.k)
    v = jnp.einsum("btd,dhv->bhtv", attn_in, layer.v)

  # Apply rotary embeddings
  with jax.named_scope("rope"):
    q = apply_rotary_embedding(q, sin, cos)
    k = apply_rotary_embedding(k, sin, cos)

  with jax.named_scope("cache_update"):
    if cache is not None:
      cache_k, cache_v = cache.k[idx], cache.v[idx]

      def update(original, update, at):
        # Axis -1 because we are in vmap.
        return jax.lax.dynamic_update_slice_in_dim(
          original, update, at, axis=cache.time_axis - 1
        )

      # TODO(sholto): Guaranteed this introduces a gather :)
      k, v = (
        jax.vmap(update, in_axes=(0, 0, 0))(
          cache_k, k.astype(cache_k.dtype), cache.lengths
        ),
        jax.vmap(update, in_axes=(0, 0, 0))(
          cache_v, v.astype(cache_v.dtype), cache.lengths
        ),
      )
      q_segment_ids = jnp.where(segment_ids != 0, 1, 0)
      time_indices = jnp.arange(0, v.shape[cache.time_axis])[None, :]  # [1, T]
      incremental_positions = jnp.sum(segment_ids != 0, axis=-1)  # [B,]
      # I.e. valid below where we've written things [B, T]
      k_segment_ids = jnp.where(
        time_indices < (cache.lengths + incremental_positions)[:, None], 1, 0
      )
      # Mask our new k and v so that its very visible and easy to test kv values being entered. Tiny perf hit b/c it is unnecessary.
      k, v = k * k_segment_ids[:, None, :, None], v * k_segment_ids[:, None, :, None]
      q_offset = cache.lengths
    else:
      q_segment_ids = segment_ids
      k_segment_ids = segment_ids
      q_offset = jnp.zeros(x.shape[0], dtype=jnp.int32)

  # Compute attention
  with jax.named_scope("attention"):
    if cfg.use_attn_kernel:
      if cache is not None:
        raise ValueError("Kernel is only for training.")
      attn_out = attention_kernel(q, k, v, q_segment_ids, k_segment_ids, cfg)
    else:
      attn_out = attention(q, k, v, q_segment_ids, k_segment_ids, q_offset, cfg)

  # Project attention output
  with jax.named_scope("projection"):
    attn_out = jnp.einsum("bhtq,hqd->btd", attn_out, layer.proj)

  # Residual connection
  with jax.named_scope("residual"):
    x = x + attn_out

  # Second RMSNorm (Pre-LN for FFN)
  with jax.named_scope("ffn_pre_norm"):
    ff_in = rms_norm(x, layer.gamma2)

  # FFN
  with jax.named_scope("ffw"):
    ff_out = jnp.einsum("btd,df->btf", ff_in, layer.w1)
    ff_out = jax.nn.gelu(ff_out)
    ff_out = jnp.einsum("btf,fd->btd", ff_out, layer.w2)

  # Residual connection
  with jax.named_scope("residual"):
    x = x + ff_out

  return x, k, v


def forward(
  x: jax.Array,
  segment_ids: jax.Array,
  weights: Weights,
  cfg: Config,
  cache: KVCache | None = None,
):
  internals = {}
  # Embed input tokens [B, T] -> [B, T D]
  x = weights.embedding[x, :]
  batch, seq_len = x.shape[0], x.shape[1]
  sin, cos = _generate_fixed_pos_embedding(cfg.key_dim, cfg.max_seq_len)

  # Apply rotary embeddings: [B, T, head_dim]
  if cache is not None:
    # For inference with cache, we need to index the positional embeddings
    start_indices = cache.lengths
  else:
    start_indices = jnp.zeros((batch,), dtype=jnp.int32)

  sin = slices_at(sin, start_indices, seq_len)
  cos = slices_at(cos, start_indices, seq_len)

  for idx, layer in enumerate(weights.layers):
    x, k, v = forward_layer(x, segment_ids, layer, sin, cos, idx, cfg, cache)
    if cache is not None:
      cache.k[idx] = k
      cache.v[idx] = v

  # Project to vocabulary size
  logits = jnp.einsum("btd,dv->btv", x, weights.vocab_proj)
  if cache is not None:
    # Sum where there is a valid segment id (i.e. non padding tokens) [B, T] -> [B,]
    cache = dataclasses.replace(
      cache, lengths=cache.lengths + jnp.sum(segment_ids != 0, axis=-1)
    )
    return logits, cache, internals
  return logits, internals


# Training.


def get_lr_with_cosine_decay_and_warmup(
  step: int, total_steps: int, max_lr: float, min_lr: float, warmup_steps: int
):
  """Calculate learning rate using cosine decay with linear warmup."""

  def warmup(s):
    return max_lr * (s / warmup_steps)

  def cosine_decay(s):
    progress = (s - warmup_steps) / (total_steps - warmup_steps)
    return min_lr + 0.5 * (max_lr - min_lr) * (1 + jnp.cos(jnp.pi * progress))

  return jax.lax.cond(step < warmup_steps, warmup, cosine_decay, step)


def adam_update(
  param: jax.Array,
  grad: jax.Array,
  m: jax.Array,
  v: jax.Array,
  lr: float,
  t: int,
  beta1=0.9,
  beta2=0.999,
  eps=1e-8,
):
  # Momentum.
  m = beta1 * m + (1 - beta1) * grad
  # Grad variance.
  v = beta2 * v + (1 - beta2) * jnp.square(grad)
  # Debiasing (helps with early training).
  m_hat = m / (1 - beta1 ** (t + 1))
  v_hat = v / (1 - beta2 ** (t + 1))
  # Adjusts the gradient update w/ momentum by the variance. Effectively
  # high variance = more cautious step, low variance = more aggressive step.
  update = lr * m_hat / (jnp.sqrt(v_hat) + eps)
  return param - update, m, v


def init_adam_state(weights: Weights):
  def _zeros_like(old):
    if isinstance(old, jax.ShapeDtypeStruct):
      return jax.ShapeDtypeStruct(old.shape, old.dtype, sharding=old.sharding)
    else:
      return jax.device_put(jnp.zeros_like(old), old.sharding)

  return jax.tree_map(lambda p: (_zeros_like(p), _zeros_like(p)), weights)


def cross_entropy_loss(
  logits: jax.Array,
  labels: jax.Array,
  mask: jax.Array,
  internals: jax.Array | None = None,
) -> tuple[jax.Array, jax.Array] | tuple[jax.Array, jax.Array, Any]:
  num_classes = logits.shape[-1]
  labels_one_hot = jax.nn.one_hot(labels, num_classes)
  log_probs = jax.nn.log_softmax(logits, axis=-1)
  loss = -jnp.sum(labels_one_hot * log_probs, axis=-1)
  loss *= mask

  if internals is not None:
    internals["per_token_loss"] = loss

  valid_tokens = jnp.sum(mask)
  # Compute mean over valid values.
  loss = loss.sum() / valid_tokens

  predictions = jnp.argmax(logits, axis=-1)
  correct_predictions = jnp.sum((predictions == labels) * mask)
  accuracy = correct_predictions / valid_tokens

  return (loss, accuracy) if internals is None else (loss, accuracy, internals)


def compute_loss(
  weights: Weights, x: jax.Array, segment_ids: jax.Array, y: jax.Array, cfg: Config
) -> tuple[jax.Array, Any]:
  logits, internals = forward(x, segment_ids, weights, cfg)
  # Important assumption that segment_ids 0 is 'padding'.
  loss_mask = jnp.where(segment_ids == 0, 0, 1)
  loss, accuracy, internals = cross_entropy_loss(logits, y, loss_mask, internals)
  internals["accuracy"] = accuracy
  return loss, internals


def update_weights(weights: Weights, grads: Weights, state: Any, lr: float, t: int):
  def update_fn(param, grad, state):
    m, v = state
    param_update, m_new, v_new = adam_update(param, grad, m, v, lr, t)
    return param_update, (m_new, v_new)

  updated = jax.tree_map(update_fn, weights, grads, state)
  # Use weights for it's tree prefix.
  new_weights = jax.tree.map(lambda _, u: u[0], weights, updated)
  new_state = jax.tree.map(lambda _, u: u[1], weights, updated)
  return new_weights, new_state


def update_step(
  weights: Weights,
  x: jax.Array,
  segment_ids: jax.Array,
  y: jax.Array,
  opt_state: Any,
  step: int,
  cfg: Config,
):
  (loss, internals), grads = jax.value_and_grad(compute_loss, has_aux=True)(
    weights, x, segment_ids, y, cfg
  )
  lr = get_lr_with_cosine_decay_and_warmup(
    step, cfg.total_steps, cfg.max_lr, cfg.min_lr, cfg.warmup_steps
  )
  weights, opt_state = update_weights(weights, grads, opt_state, lr, step)
  internals["grad_norms"] = jax.tree.map(jnp.linalg.norm, grads)
  internals["lr"] = lr
  return loss, weights, opt_state, internals


def input_shardings(
  mesh, rules
) -> tuple[
  jax.sharding.NamedSharding, jax.sharding.NamedSharding, jax.sharding.NamedSharding
]:
  logical_axes = {
    "x": P("batch", "sequence"),
    "segment_ids": P("batch", "sequence"),
    "y": P("batch", "sequence"),
  }
  return jax.tree.map(
    functools.partial(_logical_to_sharding, mesh=mesh, rules=rules), logical_axes
  )


# Checkpointing logic
def make_mgnr(path="/tmp/checkpoint_manager_sharded", erase: bool = False):
  if erase:
    path = ocp.test_utils.erase_and_create_empty(path)
  options = ocp.CheckpointManagerOptions(max_to_keep=3)
  mngr = ocp.CheckpointManager(path, options=options)
  return mngr


def save(mngr: ocp.CheckpointManager, weights: Weights, opt_state: Any, step: int):
  mngr.save(
    step, args=ocp.args.StandardSave({"weights": weights, "opt_state": opt_state})
  )
  mngr.wait_until_finished()


def load(mngr: ocp.CheckpointManager, cfg: Config, step: int | None = None):
  abstract_weights = Weights.abstract(cfg, cfg.mesh, cfg.rules)
  abstract_opt_state = init_adam_state(abstract_weights)
  restored = mngr.restore(
    mngr.latest_step() if step is None else step,
    args=ocp.args.StandardRestore(
      {"weights": abstract_weights, "opt_state": abstract_opt_state}
    ),
  )
  return restored["weights"], restored["opt_state"]


# Inference.
def prepare_chunk(chunk, pad_to: int, pad_id: int):
  # [length] -> [1, padded]
  chunk = jnp.pad(chunk, (0, pad_to - len(chunk)))[None, :]
  segment_ids = jnp.where(chunk != pad_id, 1, 0).astype(jnp.int32)
  return chunk, segment_ids


def sample_next_token(logits, temperature=1.0, greedy: bool = True):
  if greedy:
    return jnp.argmax(logits, -1)
  else:
    # Apply temperature
    logits = logits / temperature
    # Convert to probabilities
    probs = jax.nn.softmax(logits, axis=-1)
    # Sample from the distribution
    return jax.random.categorical(jax.random.PRNGKey(0), probs, axis=-1)


def sample_from_prompt(
  tokens: jax.Array,
  weights: Weights,
  cache: KVCache,
  cfg: Config,
  batch_idx: int = 0,
  num_steps: int = 20,
  greedy: bool = True,
):
  """Samples from a prompt."""

  # Calculate the next power of 2 for padding, up to cfg.max_seq.
  assert len(tokens) <= cfg.max_seq_len
  pad_to = 2 ** math.ceil(math.log2((len(tokens))))
  prompt, prompt_segment_ids = prepare_chunk(tokens, pad_to=pad_to, pad_id=0)
  cache = dataclasses.replace(
    cache,
    lengths=jax.lax.dynamic_update_index_in_dim(cache.lengths, 0, batch_idx, axis=0),
  )
  logits, cache, _ = jax.jit(forward, static_argnames="cfg")(
    prompt, prompt_segment_ids, weights, cfg, cache
  )
  next_token_logit = logits[batch_idx, cache.lengths[batch_idx] - 1, :]

  tokens = []
  for _ in range(0, num_steps):
    next_token = sample_next_token(next_token_logit, greedy=greedy)[None]
    tokens.append(next_token[0])
    prompt, prompt_segment_ids = prepare_chunk(next_token, pad_to=1, pad_id=0)
    logits, cache, _ = jax.jit(forward, static_argnames="cfg")(
      prompt, prompt_segment_ids, weights, cfg, cache
    )
    next_token_logit = logits[batch_idx, 0, :]
  return tokens, cache
