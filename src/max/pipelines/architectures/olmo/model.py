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

from __future__ import annotations

from typing import Literal

from max.engine import InferenceSession
from max.pipelines import PipelineConfig
from transformers import AutoConfig

from ..llama3.model import LlamaModelBase


class OlmoModel(LlamaModelBase):
    """Olmo pipeline model implementation."""

    norm_method: Literal["rms_norm"] | Literal["layer_norm"] = "layer_norm"

    def __init__(
        self,
        pipeline_config: PipelineConfig,
        session: InferenceSession,
        huggingface_config: AutoConfig,
    ) -> None:
        super().__init__(pipeline_config, session, huggingface_config)
