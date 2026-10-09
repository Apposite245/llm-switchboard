"""Declarative llama-server flag schema.

This is the extension point: to expose a new llama.cpp flag, append one FlagSpec to
SPECS. The UI generates its control automatically and the launcher emits it into argv.
No UI code needs to change.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

CREATE_NO_WINDOW = 0x08000000

KIND_BOOL = "bool"
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_TEXT = "text"
KIND_PATH = "path"
KIND_CHOICE = "choice"

UNSET = ""  # choice sentinel: emit nothing, let llama.cpp use its own default

GROUP_ORDER = [
    "Server", "Context & KV", "GPU & Memory", "Multimodal",
    "Speculative", "Sampling", "Chat", "Advanced",
]

CACHE_TYPES = (UNSET, "f32", "f16", "bf16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0", "iq4_nl")
SPEC_TYPES = (UNSET, "none", "draft-simple", "draft-eagle3", "draft-mtp", "draft-dflash",
              "draft-dspark", "ngram-simple", "ngram-map-k", "ngram-map-k4v", "ngram-mod", "ngram-cache")


@dataclass(frozen=True)
class FlagSpec:
    key: str
    cli: str
    kind: str
    label: str
    group: str
    default: Any = None
    choices: Sequence[str] = ()
    emit_map: dict[str, list[str]] | None = None
    help: str = ""
    minimum: float = 0
    maximum: float = 1_000_000
    step: float = 1
    decimals: int = 2
    placeholder: str = ""
    file_filter: str = "GGUF models (*.gguf);;All files (*.*)"
    advanced: bool = False
    quick: bool = False  # also surfaced on the Launch tab
    always: bool = False  # emit even when the value equals the default

    def emit(self, value: Any) -> list[str]:
        """Render this flag as argv tokens for a given value ([] = omit)."""
        if value is None:
            return []
        if self.kind == KIND_BOOL:
            if bool(value) == bool(self.default) and not self.always:
                return []
            return (self.emit_map or {}).get("on" if value else "off", [self.cli] if value else [])
        if self.kind == KIND_CHOICE:
            value = str(value)
            if value == UNSET or (value == self.default and not self.always):
                return []
            if self.emit_map:
                return list(self.emit_map.get(value, []))
            return [self.cli, value]
        if self.kind in (KIND_TEXT, KIND_PATH):
            value = str(value).strip()
            return [self.cli, value] if value else []
        # numeric
        if value == self.default and not self.always:
            return []
        if self.kind == KIND_INT:
            return [self.cli, str(int(value))]
        return [self.cli, f"{float(value):g}"]


SPECS: list[FlagSpec] = [
    # Every help string is a hover tooltip. Each says what the flag does, what it costs or
    # changes (output, speed, memory or only the API), and what the default is. The wording
    # matches the controls, which show "default" for a flag that is not sent.
    # Defaults quoted are the ones llama-server's own --help reports.

    # ---- Server -------------------------------------------------------------
    FlagSpec("host", "--host", KIND_TEXT, "Host", "Server", default="127.0.0.1",
             placeholder="127.0.0.1", quick=True, always=True,
             help="The network address the server listens on. 127.0.0.1 means only programs on "
                  "this computer can reach it; 0.0.0.0 opens it to every device on your "
                  "network.\n"
                  "No effect on the model. In router mode this is the router's address and is "
                  "taken from the All models settings."),
    FlagSpec("port", "--port", KIND_INT, "Port", "Server", default=8080,
             minimum=1, maximum=65535, quick=True, always=True,
             help="The port clients connect to. It has to match the address your client programs "
                  "use, and must not clash with another engine's port. In router mode it is "
                  "taken from the All models settings.\n"
                  "No effect on the model. Always written out explicitly, because llama.cpp's own "
                  "default port is changing to 9931."),
    FlagSpec("alias", "--alias", KIND_TEXT, "Alias", "Server",
             placeholder="name reported to API clients", quick=True,
             help="The model name the server reports in /v1/models and accepts in requests. "
                  "Several names can be given, separated by commas.\n"
                  "No effect on the model. Ignored in router mode, where the router names each "
                  "model itself. Set per model only."),
    FlagSpec("api_key", "--api-key", KIND_TEXT, "API key", "Server",
             placeholder="leave empty for no auth",
             help="A password for the API. When set, every request must send it in the "
                  "Authorization header or it is refused.\n"
                  "No effect on the model. Every client then has to be configured with the key; "
                  "one that cannot send it stops working. In router mode it is taken from the "
                  "All models settings."),
    FlagSpec("metrics", "--metrics", KIND_BOOL, "Prometheus /metrics", "Server", default=False,
             help="Adds a /metrics page with counters such as tokens processed and speed, in the "
                  "format the Prometheus monitoring tool reads.\n"
                  "No effect on the model. Off unless ticked. Ignored in router mode."),
    FlagSpec("no_webui", "--no-webui", KIND_BOOL, "Disable built-in web UI", "Server", default=False,
             help="Turns off the chat page llama-server serves in a browser at its address. The "
                  "API keeps working.\n"
                  "No effect on the model. The web UI is on unless this is ticked. Ignored in "
                  "router mode."),
    FlagSpec("slot_save_path", "--slot-save-path", KIND_PATH, "Slot cache folder", "Server",
             file_filter="",
             help="A folder where a conversation's processed state (its KV cache) can be saved to "
                  "disk and loaded back through the API, so a long prompt does not have to be "
                  "read again.\n"
                  "No effect on output. Saving is disabled while this is empty, and the files can "
                  "be several GB each."),
    FlagSpec("threads", "--threads", KIND_INT, "CPU threads", "Server", minimum=0, maximum=512,
             help="How many CPU threads do the work that runs on the processor.\n"
                  "Speed only. With the whole model on the GPU it barely matters; with layers or "
                  "MoE experts kept on the CPU it matters a lot, and the number of physical cores "
                  "is usually best. At default, llama.cpp picks for itself."),

    # ---- Context & KV -------------------------------------------------------
    FlagSpec("ctx_size", "--ctx-size", KIND_INT, "Context size", "Context & KV", default=0,
             minimum=0, maximum=10_000_000, step=1024, quick=True, always=True,
             help="How many tokens the model can hold at once: the prompt, images, thinking and "
                  "the reply together. The default (0) uses the model's trained maximum, which "
                  "auto-fit shrinks if it does not fit in VRAM.\n"
                  "More context costs more VRAM and does not change quality by itself, but a "
                  "request that does not fit is cut short or refused. Always written out "
                  "explicitly, so what you set here is what the server gets."),
    FlagSpec("cache_type_k", "--cache-type-k", KIND_CHOICE, "KV cache type (K)", "Context & KV",
             default=UNSET, choices=CACHE_TYPES, quick=True,
             help="The number format used to store the 'key' half of the model's memory of the "
                  "conversation (the KV cache).\n"
                  "Smaller formats save VRAM at a cost in accuracy: q8_0 roughly halves that half "
                  "and is close to f16 in quality, while the q4 and q5 types save more and can "
                  "visibly hurt long conversations. The default is f16."),
    FlagSpec("cache_type_v", "--cache-type-v", KIND_CHOICE, "KV cache type (V)", "Context & KV",
             default=UNSET, choices=CACHE_TYPES, quick=True,
             help="The number format used to store the 'value' half of the KV cache. Same "
                  "trade-off as the K setting: q8_0 saves VRAM with little quality loss, q4 and "
                  "q5 save more and lose more.\n"
                  "A quantised V cache needs flash attention on. The default is f16."),
    FlagSpec("flash_attn", "--flash-attn", KIND_CHOICE, "Flash attention", "Context & KV",
             default=UNSET, choices=(UNSET, "on", "off", "auto"), quick=True,
             help="A faster, leaner way of computing attention on the GPU.\n"
                  "Speed and VRAM only; the output is the same apart from tiny rounding "
                  "differences. Needed for a quantised V cache. The default is auto, which turns it "
                  "on wherever the GPU backend supports it."),
    FlagSpec("parallel", "--parallel", KIND_INT, "Server slots", "Context & KV", default=-1,
             minimum=-1, maximum=64,
             help="How many requests the server can work on at the same time. Each one gets a "
                  "'slot'.\n"
                  "No effect on output. With a fixed number above 1 and the unified KV buffer "
                  "off, the context size is divided between the slots, so each request gets only "
                  "a share. 1 gives the whole context to one request at a time. At default, "
                  "llama.cpp chooses."),
    FlagSpec("kv_unified", "--kv-unified", KIND_CHOICE, "Unified KV buffer", "Context & KV",
             default=UNSET, choices=(UNSET, "on", "off"),
             emit_map={"on": ["--kv-unified"], "off": ["--no-kv-unified"]},
             help="On: all slots share one pool of context, so a single request can use all of "
                  "it. Off: the context is split into equal fixed parts, one per slot.\n"
                  "No effect on output. The default is on when the number of slots is left on "
                  "automatic, off when you set a number."),
    FlagSpec("context_shift", "--context-shift", KIND_CHOICE, "Context shift", "Context & KV",
             default=UNSET, choices=(UNSET, "on", "off"),
             emit_map={"on": ["--context-shift"], "off": ["--no-context-shift"]},
             help="What happens when the context fills up while the model is writing. On: the "
                  "oldest tokens are thrown away so it can keep going. Off: generation stops.\n"
                  "Changes output when it triggers, because the model forgets the start of the "
                  "conversation. The default is off."),
    FlagSpec("batch_size", "--batch-size", KIND_INT, "Batch size", "Context & KV", minimum=0, maximum=131072,
             help="The most prompt tokens the server queues for processing in one go.\n"
                  "Speed of prompt reading only, no effect on output. It rarely needs changing; "
                  "the micro-batch size below is the one that affects VRAM. The default is "
                  "2048."),
    FlagSpec("ubatch_size", "--ubatch-size", KIND_INT, "Micro-batch size", "Context & KV",
             minimum=0, maximum=131072,
             help="How many prompt tokens are pushed through the model in a single step.\n"
                  "Larger can read long prompts faster but needs more working VRAM; smaller "
                  "saves VRAM. No effect on output. The default is 512."),
    FlagSpec("keep", "--keep", KIND_INT, "Keep tokens", "Context & KV", minimum=0, maximum=1_000_000,
             help="How many tokens from the start of the prompt are protected when context shift "
                  "throws old tokens away. Use it to keep the system prompt.\n"
                  "Only matters with context shift on. The default is 0, so nothing is "
                  "protected."),

    # ---- GPU & Memory -------------------------------------------------------
    FlagSpec("n_gpu_layers", "--gpu-layers", KIND_INT, "GPU layers", "GPU & Memory", default=-1,
             minimum=-1, maximum=1024, quick=True,
             help="How many of the model's layers are stored in VRAM. The rest run on the CPU "
                  "from system RAM, which is far slower. A number above the model's layer count, "
                  "such as 99, means all of them.\n"
                  "Speed and VRAM only, no effect on output. At default, auto-fit or "
                  "llama.cpp decides."),
    FlagSpec("fit", "--fit", KIND_CHOICE, "Auto-fit to VRAM", "GPU & Memory",
             default=UNSET, choices=(UNSET, "on", "off"), quick=True,
             help="On: before loading, the server measures free VRAM and adjusts anything you "
                  "left at default, mainly GPU layers and context size, so the model fits. Off: it "
                  "uses exactly what you set and fails if that does not fit.\n"
                  "No effect on output, but it can quietly give you fewer GPU layers or less "
                  "context than you expected, and the measuring adds to load time. The default is "
                  "on."),
    FlagSpec("fit_ctx", "--fit-ctx", KIND_INT, "Auto-fit min context", "GPU & Memory",
             minimum=0, maximum=10_000_000, step=1024,
             help="The smallest context size auto-fit is allowed to shrink to. Below that it "
                  "moves layers to the CPU instead.\n"
                  "Only used while auto-fit is on. The default is 4096."),
    FlagSpec("fit_target", "--fit-target", KIND_TEXT, "Auto-fit VRAM margin (MiB)", "GPU & Memory",
             placeholder="per-device, comma separated",
             help="How much VRAM, in MiB, auto-fit leaves free on each GPU for other programs. "
                  "One number applies to every GPU.\n"
                  "Only used while auto-fit is on. A bigger margin leaves room for other GPU "
                  "programs but may push layers onto the CPU. The default is 1024."),
    FlagSpec("n_cpu_moe", "--n-cpu-moe", KIND_INT, "MoE layers on CPU", "GPU & Memory",
             minimum=0, maximum=1024,
             help="For Mixture-of-Experts (MoE) models only: keeps the "
                  "expert weights of the first N layers in system RAM instead of VRAM.\n"
                  "Cuts VRAM a great deal and costs a lot of speed, often several times slower "
                  "once most layers are moved. No effect on "
                  "output, and no effect at all on ordinary dense models."),
    FlagSpec("split_mode", "--split-mode", KIND_CHOICE, "Multi-GPU split", "GPU & Memory",
             default=UNSET, choices=(UNSET, "none", "layer", "row"),
             help="How a model is divided between several GPUs: none uses one GPU, layer puts "
                  "whole layers on each, row splits each weight across them.\n"
                  "Does nothing with a single GPU. The default is layer."),
    FlagSpec("main_gpu", "--main-gpu", KIND_INT, "Main GPU", "GPU & Memory", default=-1,
             minimum=-1, maximum=16,
             help="Which GPU holds the model when split mode is none, or the working data and "
                  "KV cache when it is row. GPUs are numbered from 0.\n"
                  "Does nothing with a single GPU. The default is GPU 0."),
    FlagSpec("tensor_split", "--tensor-split", KIND_TEXT, "Tensor split", "GPU & Memory",
             placeholder="e.g. 3,1",
             help="The share of the model each GPU gets, as proportions. 3,1 puts three quarters "
                  "on the first GPU and one quarter on the second.\n"
                  "Does nothing with a single GPU."),
    FlagSpec("device", "--device", KIND_TEXT, "Devices", "GPU & Memory", placeholder="e.g. CUDA0,CUDA1",
             help="Which devices the model may be placed on, by name. 'none' keeps everything on "
                  "the CPU.\n"
                  "Speed only. Leave it empty with a single GPU and llama.cpp uses the GPU it "
                  "finds."),
    # The accepted values come from llama-server's own --load-mode help (build b10356).
    # "read" and "direct-io" were wrong and made the server refuse to start.
    FlagSpec("load_mode", "--load-mode", KIND_CHOICE, "Model load mode", "GPU & Memory",
             default=UNSET, choices=(UNSET, "auto", "none", "mmap", "mlock", "mmap+mlock", "dio"),
             help="How the model file is read from disk. Affects load time and memory use, never "
                  "the output.\n"
                  "auto (the default) memory-maps the file. dio reads it straight from the drive, "
                  "skipping the Windows file cache, which can shorten loads from a fast SSD. Use "
                  "none when weights sit on the CPU, MoE offload included, so they cannot be "
                  "evicted and re-read from disk mid-run. mlock and mmap+mlock pin the weights "
                  "in RAM so Windows cannot swap them out."),

    # ---- Multimodal ---------------------------------------------------------
    FlagSpec("mmproj", "--mmproj", KIND_PATH, "Vision projector (mmproj)", "Multimodal",
             quick=True,
             help="The companion file that lets a model see images. It turns a picture into "
                  "tokens the model can read.\n"
                  "Without it the model is text only and image requests fail. It adds load time "
                  "and VRAM, typically 0.5 to 1.5 GB. Auto-filled when an mmproj file sits "
                  "beside the model. Set per model only."),
    FlagSpec("mmproj_offload", "--mmproj-offload", KIND_CHOICE, "Projector on GPU", "Multimodal",
             default=UNSET, choices=(UNSET, "on", "off"),
             emit_map={"on": ["--mmproj-offload"], "off": ["--no-mmproj-offload"]},
             help="Where the vision projector runs. On: the GPU. Off: the CPU, which frees its "
                  "VRAM but makes every image much slower to read.\n"
                  "No effect on output. The default is on."),
    FlagSpec("image_min_tokens", "--image-min-tokens", KIND_INT, "Image min tokens", "Multimodal",
             minimum=0, maximum=16384,
             help="The fewest tokens an image is turned into. A higher floor scales small images "
                  "up so the model sees more detail.\n"
                  "Changes what the model can see, and each image uses more context and takes "
                  "longer to read. Qwen-VL grounding wants at least 1024. Only used by models "
                  "with dynamic image resolution; at default, the model's own value applies."),
    FlagSpec("image_max_tokens", "--image-max-tokens", KIND_INT, "Image max tokens", "Multimodal",
             minimum=0, maximum=65536,
             help="The most tokens a single image may take. Larger images are scaled down to "
                  "fit.\n"
                  "Lower saves context and reading time but loses fine detail such as small "
                  "text. Only used by models with dynamic image resolution; at default, the "
                  "model's own value applies."),

    # ---- Speculative --------------------------------------------------------
    FlagSpec("spec_type", "--spec-type", KIND_CHOICE, "Speculative mode", "Speculative",
             default=UNSET, choices=SPEC_TYPES, quick=True,
             help="Speculative decoding: something cheap guesses the next few tokens and the "
                  "main model checks them all in one step, keeping the ones it agrees with.\n"
                  "Speed only; quality is unchanged because every token is still verified. "
                  "draft-mtp uses a model's multi-token-prediction head, the other draft types "
                  "use a separate small model, and the ngram types reuse text already in the "
                  "context with no extra model. The default is none. Set per model only."),
    FlagSpec("spec_draft_model", "--spec-draft-model", KIND_PATH, "Draft / MTP model", "Speculative",
             quick=True,
             help="The file that makes the guesses for the draft modes: an MTP head or a small "
                  "draft model matched to this one.\n"
                  "Speed only. It uses extra VRAM, and a poorly matched draft model makes "
                  "generation slower instead of faster. Auto-filled when an MTP or draft GGUF "
                  "sits beside the model. Set per model only."),
    FlagSpec("spec_draft_ngl", "--spec-draft-ngl", KIND_INT, "Draft GPU layers", "Speculative",
             default=-1, minimum=-1, maximum=1024,
             help="How many layers of the draft model are stored in VRAM.\n"
                  "Speed and VRAM only. A draft model on the CPU is usually too slow to help. "
                  "At default, llama.cpp decides."),
    FlagSpec("spec_draft_n_max", "--spec-draft-n-max", KIND_INT, "Draft tokens (max)", "Speculative",
             minimum=0, maximum=64,
             help="The most tokens guessed ahead in each round.\n"
                  "Speed only. Higher pays off when most guesses are accepted and wastes work "
                  "when they are not, so the best value depends on the model. The default "
                  "is 3."),
    FlagSpec("spec_draft_n_min", "--spec-draft-n-min", KIND_INT, "Draft tokens (min)", "Speculative",
             minimum=0, maximum=64,
             help="The fewest guessed tokens worth checking. A round that produces fewer is "
                  "skipped and the main model generates normally.\n"
                  "Speed only. The default is 0."),
    FlagSpec("spec_draft_type_k", "--spec-draft-type-k", KIND_CHOICE, "Draft KV type (K)", "Speculative",
             default=UNSET, choices=CACHE_TYPES, advanced=True,
             help="The number format for the 'key' half of the draft model's own KV cache.\n"
                  "Smaller formats save a little VRAM. They cannot lower output quality, only "
                  "how many guesses are accepted. The default is f16."),
    FlagSpec("spec_draft_type_v", "--spec-draft-type-v", KIND_CHOICE, "Draft KV type (V)", "Speculative",
             default=UNSET, choices=CACHE_TYPES, advanced=True,
             help="The number format for the 'value' half of the draft model's own KV cache.\n"
                  "Smaller formats save a little VRAM. They cannot lower output quality, only "
                  "how many guesses are accepted. The default is f16."),

    # ---- Sampling -----------------------------------------------------------
    # These are server defaults. A request that sends its own value wins.
    FlagSpec("temp", "--temp", KIND_FLOAT, "Temperature", "Sampling", minimum=0, maximum=5, step=0.05,
             help="How random the choice of each next token is. Low values are focused and "
                  "repeatable, high values more varied and more error-prone; 0 always takes the "
                  "most likely token.\n"
                  "Changes output. Used only when a request does not send its own temperature, "
                  "and most chat clients and agents do send one. The default is 0.8."),
    FlagSpec("top_k", "--top-k", KIND_INT, "Top-K", "Sampling", minimum=0, maximum=1000,
             help="Only the K most likely next tokens are considered. Lower is safer and more "
                  "predictable; 0 turns the limit off.\n"
                  "Changes output. Used only when a request does not send its own value. The "
                  "default is 40."),
    FlagSpec("top_p", "--top-p", KIND_FLOAT, "Top-P", "Sampling", minimum=0, maximum=1, step=0.01,
             help="Only the most likely tokens that together add up to this probability are "
                  "considered. 0.9 drops the unlikely 10% tail; 1.0 turns it off.\n"
                  "Changes output. Used only when a request does not send its own value. The "
                  "default is 0.95."),
    FlagSpec("min_p", "--min-p", KIND_FLOAT, "Min-P", "Sampling", minimum=0, maximum=1, step=0.01,
             help="Drops any token less likely than this fraction of the top token's "
                  "probability. Higher cuts more of the unlikely choices; 0 turns it off.\n"
                  "Changes output. Used only when a request does not send its own value. The "
                  "default is 0.05."),
    FlagSpec("repeat_penalty", "--repeat-penalty", KIND_FLOAT, "Repeat penalty", "Sampling",
             minimum=0, maximum=4, step=0.01,
             help="Makes tokens that appeared recently less likely to be chosen again. 1.0 is "
                  "off.\n"
                  "Changes output. A little (around 1.05 to 1.1) curbs loops; too much damages "
                  "code and structured output such as JSON, which have to repeat tokens. "
                  "Used only when a request does not send its own value. The default is 1.0."),
    FlagSpec("seed", "--seed", KIND_INT, "Seed", "Sampling", default=-1, minimum=-1, maximum=2_147_483_647,
             help="The starting number for the random choices. The same seed with the same "
                  "prompt and settings gives the same reply.\n"
                  "Changes which reply you get, not how good it is. Used only when a request does "
                  "not send its own seed. At default, a new random seed is picked every time."),

    # ---- Chat ---------------------------------------------------------------
    FlagSpec("jinja", "--jinja", KIND_CHOICE, "Jinja chat template", "Chat",
             default=UNSET, choices=(UNSET, "on", "off"), quick=True,
             emit_map={"on": ["--jinja"], "off": ["--no-jinja"]},
             help="Whether the server formats chats with the model's full Jinja template. The "
                  "template is what turns messages, tools and thinking settings into the exact "
                  "text the model was trained on.\n"
                  "Changes output. Off falls back to a simplified built-in format and breaks "
                  "tool calls and thinking control. The default is on."),
    FlagSpec("reasoning", "--reasoning", KIND_CHOICE, "Reasoning", "Chat",
             default=UNSET, choices=(UNSET, "on", "off"),
             help="Whether the model thinks before it answers. On gives better answers on hard "
                  "tasks but is slower and spends tokens on the thinking; off answers at once.\n"
                  "Changes output. Used only when a request does not say whether to think. The default is auto, "
                  "which follows the model's template. Replaces the deprecated enable_thinking "
                  "chat-template kwarg."),
    FlagSpec("reasoning_format", "--reasoning-format", KIND_CHOICE, "Reasoning format", "Chat",
             default=UNSET, choices=(UNSET, "none", "auto", "deepseek", "deepseek-legacy"),
             help="Where the thinking text goes in the API reply. none leaves it inside the "
                  "normal message; deepseek moves it to a separate reasoning_content field; "
                  "deepseek-legacy does both.\n"
                  "The model generates the same text either way; this only changes what clients "
                  "receive. Agents expect the separate field. The default is auto."),
    FlagSpec("reasoning_budget", "--reasoning-budget", KIND_INT, "Reasoning budget", "Chat",
             default=-1, minimum=-1, maximum=1_000_000,
             help="The most tokens the model may spend thinking before it has to answer. 0 ends "
                  "thinking immediately.\n"
                  "Changes output: a tight budget speeds up replies and can cut reasoning short "
                  "on hard tasks. The default is no limit."),
    FlagSpec("chat_template", "--chat-template", KIND_TEXT, "Chat template", "Chat",
             placeholder="built-in template name",
             help="Replaces the model's own chat template with one of llama.cpp's built-in ones "
                  "by name, such as chatml or gemma.\n"
                  "Changes output. A wrong template makes a model ramble, repeat itself or "
                  "ignore the system prompt. Leave it empty to use the template stored in the "
                  "model file, which is almost always right."),
    FlagSpec("chat_template_file", "--chat-template-file", KIND_PATH, "Chat template file", "Chat",
             file_filter="Jinja templates (*.jinja *.j2 *.txt);;All files (*.*)",
             help="Replaces the model's own chat template with a Jinja file from disk. Useful "
                  "when the template inside a GGUF is broken or out of date.\n"
                  "Changes output, with the same risk as a wrong built-in template. Leave it "
                  "empty to use the template stored in the model file."),

    # ---- Advanced -----------------------------------------------------------
    FlagSpec("cont_batching", "--cont-batching", KIND_CHOICE, "Continuous batching", "Advanced",
             default=UNSET, choices=(UNSET, "on", "off"), advanced=True,
             emit_map={"on": ["--cont-batching"], "off": ["--no-cont-batching"]},
             help="Lets a new request join the batch while others are still generating, instead "
                  "of waiting for them to finish.\n"
                  "Only matters with more than one slot. No effect on output. The default is on."),
    FlagSpec("ctx_checkpoints", "--ctx-checkpoints", KIND_INT, "Context checkpoints", "Advanced",
             minimum=0, maximum=256, advanced=True,
             help="How many snapshots of the conversation state each slot keeps. When a new "
                  "request changes something earlier in the chat, the server rewinds to a "
                  "snapshot instead of reading the whole prompt again.\n"
                  "No effect on output. More snapshots use more system RAM and save re-reading "
                  "time in long agent sessions. The default is 32."),
    FlagSpec("kv_unified_per_slot", "--kv-unified-per-slot", KIND_INT, "KV per slot", "Advanced",
             minimum=0, maximum=10_000_000, step=1024, advanced=True,
             help="A context limit for each slot when the unified KV buffer is shared, so one "
                  "request cannot take the whole pool. Set without a context size, the pool "
                  "becomes slots times this number.\n"
                  "No effect on output. At default there is no per-slot limit."),
    FlagSpec("override_tensor", "--override-tensor", KIND_TEXT, "Override tensor buffers", "Advanced",
             placeholder="pattern=buffer", advanced=True,
             help="Places specific weights by hand: every tensor whose name matches the pattern "
                  "goes to the named device, for example a pattern followed by =CPU.\n"
                  "Speed and VRAM only, no effect on output. For expert use; a wrong pattern "
                  "can put hot weights on the CPU and make the model crawl."),
    FlagSpec("numa", "--numa", KIND_CHOICE, "NUMA policy", "Advanced",
             default=UNSET, choices=(UNSET, "distribute", "isolate", "numactl"), advanced=True,
             help="Thread placement for machines with more than one CPU socket or memory bank.\n"
                  "Does nothing on an ordinary single-CPU computer. Leave it at default."),
    FlagSpec("verbose", "--verbose", KIND_BOOL, "Verbose logging", "Advanced", default=False, advanced=True,
             help="Logs everything the server does, including every prompt and each load step "
                  "with timings.\n"
                  "No effect on output. The log becomes very large and slows the server a "
                  "little, so turn it on only while hunting a problem."),
]

SPECS_BY_KEY = {s.key: s for s in SPECS}


def defaults() -> dict[str, Any]:
    return {s.key: s.default for s in SPECS if s.default is not None}


def build_argv(binary: str | Path, model: str | Path, values: dict[str, Any],
               extra: str = "", specs: Sequence[FlagSpec] = SPECS) -> list[str]:
    """Full argv for llama-server, in a stable, readable order."""
    argv: list[str] = [str(binary), "--model", str(model)]
    for spec in specs:
        if spec.key in values:
            argv.extend(spec.emit(values[spec.key]))
    if extra.strip():
        argv.extend(split_extra(extra))
    return argv


def split_extra(text: str) -> list[str]:
    """Split a free-text extra-args box into argv tokens. Double quotes group words that
    contain spaces and are removed, as a shell would: the process is started from a list,
    so a quote left in a token would reach the server as part of the value."""
    tokens = re.findall(r'(?:[^\s"]|"[^"]*")+', text.strip())
    return [t for t in (token.replace('"', "") for token in tokens) if t]


def quote(token: str) -> str:
    return f'"{token}"' if (" " in token or "\t" in token) and not token.startswith('"') else token


def format_command(argv: Sequence[str], multiline: bool = False) -> str:
    tokens = [quote(t) for t in argv]
    if not multiline:
        return " ".join(tokens)
    lines = [tokens[0]]
    i = 1
    while i < len(tokens):
        if tokens[i].startswith("-") and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
            lines.append(f"{tokens[i]} {tokens[i + 1]}")
            i += 2
        else:
            lines.append(tokens[i])
            i += 1
    return " `\n  ".join(lines)


_FLAG_TOKEN_RE = re.compile(r"--[A-Za-z0-9][\w-]*")
_DEF_LINE_RE = re.compile(r"^\s{0,4}-{1,2}[A-Za-z0-9]")
# llama.cpp pads the flag list to a fixed width and starts descriptions here.
DESC_COL = 40


def _split_definition(line: str) -> tuple[str, str]:
    """Separate the flag list from the description on a help definition line."""
    if (len(line) > DESC_COL and line[DESC_COL - 2:DESC_COL].isspace()
            and not line[DESC_COL].isspace()):
        return line[:DESC_COL], line[DESC_COL:]
    return line, ""


def parse_help(text: str) -> set[str]:
    """Flag tokens from --help output, minus ones documented as removed.

    Deliberately a line-at-a-time parser: a multi-line regex over ~1500 lines of help
    backtracks badly enough to hang. Flags are only harvested from the flag-list
    column, because descriptions name other flags ("use --spec-draft-n-max instead").
    """
    available: set[str] = set()
    removed: set[str] = set()
    block: set[str] = set()
    block_text: list[str] = []

    def flush() -> None:
        if block and any("argument has been removed" in line for line in block_text):
            removed.update(block)

    for line in text.splitlines():
        if _DEF_LINE_RE.match(line):
            flush()
            head, tail = _split_definition(line)
            block = set(_FLAG_TOKEN_RE.findall(head))
            block_text = [tail]
            available.update(block)
        else:
            block_text.append(line)
    flush()
    return available - removed


def supported_flags(binary: str | Path, timeout: int = 90) -> set[str]:
    """Flags the given llama-server build actually accepts. Slow - never call on the UI thread."""
    try:
        proc = subprocess.run([str(binary), "--help"], capture_output=True, text=True,
                              timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return set()
    return parse_help((proc.stdout or "") + (proc.stderr or ""))


def unsupported(specs_values: dict[str, Any], available: set[str],
                specs: Sequence[FlagSpec] = SPECS) -> list[FlagSpec]:
    """Specs that would emit something this build does not understand."""
    if not available:
        return []
    out = []
    for spec in specs:
        if spec.key not in specs_values:
            continue
        tokens = spec.emit(specs_values[spec.key])
        flags = [t for t in tokens if t.startswith("--")]
        if flags and any(f not in available for f in flags):
            out.append(spec)
    return out
