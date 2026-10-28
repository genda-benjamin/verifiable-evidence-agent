"""Self-host Meta's Muse Glimmer (30B, Apache 2.0) on one GPU with vLLM, on Modal, for the self-hosted evaluation.

    modal secret create vea-glimmer VLLM_API_KEY=<a long random token>   # once; put the same token in .env as VEA_API_KEY
    modal deploy evals/modal_glimmer.py                                    # prints the endpoint URL
    # in .env: VEA_BASE_URL=<that URL>/v1 and VEA_MODEL=muse-glimmer-30b, then:
    python -m vea eval evals/vsaq.json --mode agent --provider custom

This is what "a bank can run it inside its own walls" means in practice: the whole agent, answering and verifying, on a
model whose weights anyone can download, served from one GPU the operator controls. The weights are downloaded once
into a Modal volume; the server stops after ten idle minutes, so it costs nothing between runs.
"""
import modal

MODEL = "meta-models/Muse-Glimmer-30B"
SERVED_NAME = "muse-glimmer-30b"

app = modal.App("vea-glimmer")
image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("vllm", "huggingface_hub[hf_transfer]")
         # FlashInfer's sampler compiles a kernel at start-up, which needs a CUDA compiler this slim image lacks.
         .env({"HF_HUB_ENABLE_HF_TRANSFER": "1", "VLLM_USE_FLASHINFER_SAMPLER": "0"}))
weights = modal.Volume.from_name("vea-glimmer-weights", create_if_missing=True)


@app.function(image=image, gpu="H100", volumes={"/root/.cache/huggingface": weights},
              secrets=[modal.Secret.from_name("vea-glimmer")], timeout=60 * 60, scaledown_window=10 * 60, max_containers=1)
@modal.concurrent(max_inputs=8)
@modal.web_server(port=8000, startup_timeout=30 * 60)
def serve():
    import os
    import subprocess
    import threading

    server = subprocess.Popen(["vllm", "serve", MODEL, "--served-model-name", SERVED_NAME, "--host", "0.0.0.0",
                               "--port", "8000", "--max-model-len", "32768", "--api-key", os.environ["VLLM_API_KEY"]])
    # If vLLM dies, end the container at once rather than holding a paid GPU until the start-up timeout.
    threading.Thread(target=lambda: (server.wait(), os._exit(1)), daemon=True).start()
