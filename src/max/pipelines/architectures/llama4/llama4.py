# ===----------------------------------------------------------------------=== #
# Copyright (c) 2025, Modular Inc. All rights reserved.
#
# Licensed under the Apache License v2.0 with LLVM Exceptions:
# https://llvm.org/LICENSE.txt
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ===----------------------------------------------------------------------=== #
"""Implements the Llama4 model."""

from __future__ import annotations

from typing import cast

from max.dtype import DType
from max.graph import BufferValue, DeviceRef, TensorValue, TensorValueLike, ops
from max.nn import (
    ColumnParallelLinear,
    DistributedMLP,
    DistributedRMSNorm,
    Module,
    OptimizedRotaryEmbedding,
    VocabParallelEmbedding,
)
from max.nn.layer import LayerList
from max.pipelines.kv_cache import (
    ContinuousBatchingKVCacheCollection,
    FetchPagedKVCacheCollection,
    PagedKVCacheCollection,
)

from .layers.attention import Llama4TextAttention
from .mix_of_experts import MoE
from .model_config import Llama4Config


def distribute_value(v, devices: list[DeviceRef]):
    return [v.to(device) for device in devices]


class Llama4DecoderLayer(Module):
    """Llama4 decoder attention block."""

    def __init__(
        self,
        rope: OptimizedRotaryEmbedding,
        config: Llama4Config,
        layer_idx: int,
        devices: list[DeviceRef],
    ):
        super().__init__()
        if config.no_rope_layers:
            use_rope = layer_idx in config.no_rope_layers
        else:
            use_rope = (layer_idx + 1) % config.no_rope_layer_interval == 0
        self.self_attn = Llama4TextAttention(
            rope=rope,
            num_attention_heads=config.num_attention_heads,
            num_key_value_heads=config.num_key_value_heads,
            hidden_size=config.hidden_size,
            kv_params=config.kv_params,
            layer_idx=layer_idx,
            dtype=config.dtype,
            attn_temperature_tuning=config.attn_temperature_tuning,
            floor_scale=config.floor_scale,
            attn_scale=config.attn_scale,
            devices=config.devices,
            use_rope=use_rope,
            use_qk_norm=config.use_qk_norm,
        )
        self.is_moe_layer = layer_idx in config.moe_layers
        self.feed_forward: Module
        if self.is_moe_layer:
            # TODO: This is a hack to avoid overloading GPU 0. This should be
            # replaced with expert parallelism.
            self.moe_device_index = layer_idx % len(config.devices)
            self.feed_forward = MoE(
                hidden_dim=config.hidden_size,
                top_k=config.num_experts_per_tok,
                num_experts=config.num_local_experts,
                intermediate_size=config.intermediate_size,
                intermediate_size_mlp=config.intermediate_size_mlp,
                dtype=config.dtype,
                device=config.devices[self.moe_device_index],
            )
        else:
            self.feed_forward = DistributedMLP(
                config.dtype,
                quantization_encoding=None,
                hidden_dim=config.hidden_size,
                feed_forward_length=config.intermediate_size_mlp,
                devices=config.devices,
            )
        self.input_layernorm = DistributedRMSNorm(
            config.hidden_size, eps=config.rms_norm_eps, devices=config.devices
        )
        self.post_attention_layernorm = DistributedRMSNorm(
            config.hidden_size, eps=config.rms_norm_eps, devices=config.devices
        )
        self.devices = devices

    def __call__(
        self,
        xs: list[TensorValue],
        distributed_cache_positions: list[TensorValue],
        signal_buffers: list[BufferValue],
        kv_collections: list[
            ContinuousBatchingKVCacheCollection | PagedKVCacheCollection
        ],
        **kwargs,
    ) -> list[TensorValue]:
        attn_outs = self.self_attn(
            self.input_layernorm(xs),
            distributed_cache_positions,
            kv_collections,
            signal_buffers=signal_buffers,
            **kwargs,
        )

        hidden_states = [x + attn_out for x, attn_out in zip(xs, attn_outs)]
        post_norm_states = self.post_attention_layernorm(hidden_states)
        if self.is_moe_layer:
            mlp_outs = self.feed_forward(
                [post_norm_states[self.moe_device_index]],
                signal_buffers=signal_buffers,
            )
            hidden_states = distribute_value(
                mlp_outs[0] + hidden_states[self.moe_device_index], self.devices
            )
        else:
            mlp_outs = self.feed_forward(
                post_norm_states, signal_buffers=signal_buffers
            )
            hidden_states = [
                h + mlp_out for h, mlp_out in zip(hidden_states, mlp_outs)
            ]
        return hidden_states


class Llama4TextModel(Module):
    """The Llama4 text transformer model."""

    def __init__(self, config: Llama4Config):
        super().__init__()
        self.rope = OptimizedRotaryEmbedding(
            config.hidden_size,
            config.num_attention_heads,
            config.rope_theta,
            config.max_seq_len,
            interleaved=True,
        )
        self.n_heads = config.num_attention_heads
        self.layers = LayerList(
            [
                Llama4DecoderLayer(self.rope, config, layer_idx, config.devices)
                for layer_idx in range(config.num_hidden_layers)
            ]
        )
        self.norm = DistributedRMSNorm(
            config.hidden_size, eps=config.rms_norm_eps, devices=config.devices
        )
        self.lm_head = ColumnParallelLinear(
            config.hidden_size,
            config.vocab_size,
            config.dtype,
            devices=config.devices,
            quantization_encoding=None,
        )
        self.embed_tokens = VocabParallelEmbedding(
            config.vocab_size,
            config.hidden_size,
            config.dtype,
            config.devices,
            quantization_encoding=None,
        )
        self.kv_params = config.kv_params
        self.kv_collection_constructor = FetchPagedKVCacheCollection(
            config.kv_params, num_layers=config.num_hidden_layers
        )
        self.return_n_logits = config.return_n_logits
        self.devices = config.devices

        if config.return_n_logits not in (1, -1):
            raise ValueError("return_n_logits must be either -1 or 1")

    def __call__(
        self,
        tokens: TensorValueLike,
        cache_positions: TensorValueLike,
        signal_buffers: list[BufferValue],
        kv_cache_inputs_per_dev: list[tuple[TensorValue, ...]],
        **kwargs,
    ) -> tuple[TensorValue, ...]:
        h = self.embed_tokens(tokens, signal_buffers)

        kv_collections = [
            self.kv_collection_constructor(*kv_cache_inputs)
            for kv_cache_inputs in kv_cache_inputs_per_dev
        ]

        input_row_offsets = kwargs["input_row_offsets"]
        root_cache_lengths = kv_cache_inputs_per_dev[0][1]
        valid_lengths: TensorValue = ops.rebind(
            input_row_offsets[1:] - input_row_offsets[:-1],
            root_cache_lengths.shape,
        )
        context_lengths = valid_lengths + root_cache_lengths
        context_lengths = context_lengths.cast(DType.int32)
        distributed_cache_positions = distribute_value(
            cache_positions, self.devices
        )
        for _, layer in enumerate(self.layers):
            h = layer(
                h,
                distributed_cache_positions,
                signal_buffers,
                kv_collections,
                context_lengths=context_lengths,
                **kwargs,
            )

        h0 = h[0]
        last_token_indices = input_row_offsets[1:] - 1
        last_token_h = ops.gather(h0, last_token_indices, axis=0)
        last_token_distributed = distribute_value(last_token_h, self.devices)
        last_logits = ops.cast(
            self.lm_head(self.norm(last_token_distributed))[0], DType.float32
        )

        logits = None
        offsets = None

        if self.return_n_logits > 1:
            return_n_logits_range = ops.range(
                ops.constant(self.return_n_logits, DType.int64),
                ops.constant(0, DType.int64),
                ops.constant(-1, DType.int64),
                out_dim="return_n_logits_range",
            )
            offsets = (
                ops.unsqueeze(input_row_offsets[1:], -1) - return_n_logits_range
            )
            last_indices = ops.reshape(offsets, shape=(-1,))
            logits = ops.gather(
                ops.cast(self.lm_head(self.norm(h))[0], DType.float32),
                last_indices,
                axis=0,
            )
            offsets = ops.range(
                ops.constant(0, DType.int64),
                last_indices.shape[0] + self.return_n_logits,
                ops.constant(self.return_n_logits, DType.int64),
                out_dim="logit_offsets",
            )
        elif self.return_n_logits == -1:
            logits = ops.cast(self.lm_head(self.norm(h))[0], DType.float32)
            offsets = cast(TensorValue, kwargs["input_row_offsets"])
        elif self.return_n_logits == 0 or self.return_n_logits < -1:
            raise ValueError(
                f"return_n_logits provided ({self.return_n_logits}), must be greater than -1, and cannot be 0"
            )

        if logits is not None and offsets is not None:
            return (last_logits, logits, offsets)
        else:
            return (last_logits,)


class Llama4(Module):
    """The Llama4 model (currently text-only)."""

    def __init__(self, config: Llama4Config):
        self.language_model = Llama4TextModel(config)

    def __call__(
        self,
        tokens: TensorValueLike,
        cache_positions: TensorValueLike,
        signal_buffers: list[BufferValue],
        kv_cache_inputs_per_dev: list[tuple[TensorValue, ...]],
        **kwargs,
    ) -> tuple[TensorValue, ...]:
        return self.language_model(
            tokens,
            cache_positions,
            signal_buffers,
            kv_cache_inputs_per_dev,
            **kwargs,
        )
