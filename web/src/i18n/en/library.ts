// Models, the model library (pages/Models.tsx: Catalog, Engines, Sources), the pieces the model and engine pages
// share (components/LibraryPage.tsx), and the words of lib/models.ts and lib/engines.ts.
export default {
  "library.index": "03 · Models",
  "library.title": "Models",
  "library.lead":
    "The model library: the models you can deploy, the engines that run them and, for admins, where models may come from. What runs now is on Serving.",
  "library.deploy": "Deploy a model",
  "library.help.first": "How do I deploy my first model?",
  "library.help.concepts": "Models, engines and estimates",
  "library.help.download": "Download a model before deploying it",
  "library.help.offline": "Offline machines",
  "library.help.recipes": "Built-in recipes",
  "library.tab.catalog": "Catalog",
  "library.tab.catalogCount": "Catalog ({count})",
  "library.tab.engines": "Engines",
  "library.tab.sources": "Sources",

  // The catalog
  "library.models.one": "{count} model",
  "library.models.other": "{count} models",
  "library.catalog.emptyManage":
    "The catalog lists the models that can be deployed. It is empty: pick one of the recommended models above, or search Hugging Face.",
  "library.catalog.emptyUser": "The catalog lists the models that can be deployed. It is empty: an admin adds models.",
  "library.col.model": "Model",
  "library.col.task": "Task",
  "library.col.format": "Format",
  "library.col.size": "Size",
  "library.col.architecture": "Architecture",
  "library.col.licence": "Licence",
  "library.col.engines": "Engines",
  "library.removeConfirm": "Remove {name} from the catalog? Machines keep their cached copy.",
  "library.projector": " + projector",
  "library.takesTitle": "What it takes in chat besides text",
  "library.paramsSuffix": " · {params} params",
  "library.zeroShotTitle": "A zero-shot detector: each request names what to find",
  "library.findsWhatYouName": "Finds what you name",
  "library.classes": "{count} classes",
  "library.layers": "{count} layers",
  "library.kvHeads": ", {count} KV heads",
  "library.ctx": ", {count} ctx",
  "library.licenceFrom": "The licence of {source}, which this is converted from",
  "library.gated": "gated",
  "library.noneFits": "none fits",
  "library.actionsFor": "Actions for {name}",
  "library.deployShort": "Deploy",
  "library.downloadTo": "Download to…",
  "library.downloadToTitle": "Download {name} to machines now, so a deployment there starts without waiting",
  "library.remove": "Remove",
  "library.deployedFirst": "Deployed: delete its deployments first",

  // Engines
  "library.addEngine": "Add a custom engine",
  "library.engines.lead": "how each kind of model runs, per hardware",
  "library.deleteEngineConfirm": "Delete the engine {name}? A deployment that uses it must be deleted first.",
  "library.engineDeleted": "Engine {name} deleted.",
  "library.engineAdded": "Engine {name} added.",
  "library.engineSaved": "Engine {name} saved.",
  "library.engineLabel": "Engine {name}",
  "library.custom": "custom",
  "library.runsOn": "Runs on: {list}",
  "library.sharesGpu": "shares a GPU",
  "library.serves": "Serves {tasks}",
  "library.details": "Details",
  "library.formatsEstimate": "Formats: {formats} · estimate: {estimate}",
  "library.variantsOf": "Variants of {name}",
  "library.process": "process: {engine}",
  "library.cc": "cc {value}",
  "library.driver": "driver {value}",
  "library.edit": "Edit",
  "library.delete": "Delete",

  // Loading an engine's image on a machine without registry access
  "library.offline.summary": "Machine without registry access: load the image",
  "library.offline.lead":
    "On a machine that can reach the registry, save the image of the variant you need to a file. Copy it over, load it, and deploy as usual. <link>Offline machines</link>",
  "library.offline.variant": "Variant",
  "library.offline.save": "Save command for {what}",
  "library.offline.load": "Load command for {what}",
  "library.offline.checked": "Forgeyard checks the loaded image against the ids recorded for it, so this engine works offline as it is.",
  "library.offline.unchecked":
    "No ids are recorded for this image, so Forgeyard cannot check a loaded copy: name it by its tag only (without {digest}) in the recipe.",

  // Adding or changing a custom engine
  "library.form.change": "Change {name}",
  "library.form.lead":
    "Define an engine with your own image and arguments — for example a newer version, copied from a built-in engine. Built-in engines are updated with Forgeyard releases.",
  "library.form.what":
    "An engine is a recipe in YAML, like the built-in ones: the program or container image per hardware, its parameters and its memory estimate. <link>Built-in recipes</link>",
  "library.form.start": "Start from a built-in engine",
  "library.form.empty": "Empty: write the YAML yourself",
  "library.form.startHint": "The copy gets a name of its own; change the variants or parameters you need.",
  "library.form.yaml": "Engine recipe (YAML)",
  "library.form.saving": "Saving…",
  "library.form.add": "Add engine",
  "library.form.save": "Save changes",

  // Sources and the server's Hugging Face token
  "library.sources.saved": "Saved.",
  "library.sources.where":
    "Hugging Face models come from {endpoint}. Anything else only from what is listed here. Private and metadata addresses are always refused{except}.",
  "library.sources.except": " (except: private hosts are allowed on this server for a LAN mirror)",
  "library.sources.fromEnv": "from .env",
  "library.sources.seeds": "These lists come from {names}: nobody has saved them here yet. Saving replaces them, and the environment is then ignored.",
  "library.sources.hosts": "Hosts for GGUF URLs (one per line, *.example.org for subdomains)",
  "library.sources.dirs": "Directories for local:// models",
  "library.sources.dirsHint": "Agents must also allow them (--model-local-dir).",
  "library.sources.save": "Save sources",
  "library.hf.title": "Hugging Face token",
  "library.hf.lead":
    "For gated models (Llama, Gemma 3, FLUX.1-schnell…): used to inspect them and sent to the agent that downloads them, only to Hugging Face. A deployment's own token (Deploy a model → Advanced) wins for that deployment. Stored encrypted; never shown again.",
  "library.hf.set": "<chip>set</chip> ends in {last4}",
  "library.hf.by": " · by {name}",
  "library.hf.notSet": "<chip>not set</chip> Gated models need a token, here or on each deployment.",
  "library.hf.replace": "Replace the token",
  "library.hf.token": "Token",
  "library.hf.hint": "A read token of an account that accepted the model's licence on huggingface.co.",
  "library.hf.save": "Save token",
  "library.hf.remove": "Remove token",
  "library.hf.saved": "Token saved.",
  "library.hf.removed": "Token removed.",

  // components/LibraryPage.tsx
  "library.noMachines": "No machine has joined yet. <link>Add a machine</link>.",
  "library.onThisPage": "On this page",
  "library.fold.show": "Show {what}",
  "library.fold.hide": "Hide {what}",
  "library.deployedCount": " · {count} deployed",

  // lib/models.ts: what a model takes besides text, its size in parameters
  "library.medium.image": "Images",
  "library.medium.audio": "Audio",
  "library.medium.video": "Video",
  "library.textOnly": "Text only",
  "library.listSep": ", ",

  // The built-in engines' descriptions (the recipe files' English, as the server sends it): shown translated only
  // while the server still sends this exact text, else as sent.
  "library.engineDesc.faster-whisper":
    "Speech recognition with faster-whisper models (CTranslate2) behind an OpenAI-compatible /v1/audio/transcriptions. Small; shares a GPU by default.",
  "library.engineDesc.llama-cpp":
    "GGUF (quantised) models with llama-server: on NVIDIA GPUs, AMD GPUs (ROCm, or Vulkan for APUs and other cards) in a container, or on the CPU in a container or as a plain process. OpenAI-compatible API, for chat and for text to speech (Qwen3-TTS, Chatterbox and other TTS GGUFs with their projector).",
  "library.engineDesc.sd-cpp":
    "Text to image with stable-diffusion.cpp: AMD GPUs and APUs (Vulkan) and NVIDIA GPUs in a container, or the CPU as a plain process. GGUF and single-file models (Stable Diffusion 1.5 / XL, Z-Image, FLUX.1), behind OpenAI's /v1/images/generations.",
  "library.engineDesc.speaches-tts":
    "Small English voices (Kokoro-82M, Piper) behind an OpenAI-compatible /v1/audio/speech; fast enough on a CPU, and shares a GPU by default. English (and other Latin-script) text only.",
  "library.engineDesc.transformers":
    "Image embeddings (CLIP, SigLIP, SigLIP 2, DINOv2) at /v1/embeddings, and text classification with fixed labels or zero-shot with NLI models at /v1/classify. Small: runs well on a CPU, shares a GPU by default.",
  "library.engineDesc.vision":
    "Object detection, segmentation (SAM 2), depth estimation and image classification with transformers models, behind /v1/vision/detect, /v1/vision/segment, /v1/vision/depth and /v1/vision/classify. Small: shares a GPU by default, and runs well on a CPU.",
  "library.engineDesc.vllm-omni-image":
    "Text to image on NVIDIA GPUs with vLLM-Omni: diffusers pipelines such as Z-Image-Turbo, Stable Diffusion XL and FLUX.1-schnell, behind OpenAI's /v1/images/generations (base64 images).",
  "library.engineDesc.vllm-omni-tts":
    "Qwen3-TTS on NVIDIA GPUs with vLLM-Omni: built-in voices that speak Chinese (Mandarin and dialects), English and eight more languages, with style instructions. OpenAI-compatible /v1/audio/speech with streaming.",
  "library.engineDesc.vllm-omni-video":
    "Text to video on NVIDIA GPUs with vLLM-Omni: diffusers pipelines such as Wan2.1-T2V-1.3B and Wan2.2-TI2V-5B, behind OpenAI's /v1/videos (asynchronous jobs, MP4 files).",
  "library.engineDesc.vllm-pooling":
    "Embedding, reranker and text classification models on vLLM's pooling runner. Small enough to share one GPU: each replica reserves its estimate and vLLM is held to it.",
  "library.engineDesc.vllm":
    "Chat and completion models from Hugging Face safetensors checkpoints on NVIDIA or AMD GPUs, with an OpenAI-compatible API. Memory is reserved from the estimate and enforced through --gpu-memory-utilization.",
} as const;
