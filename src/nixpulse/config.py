import os
from pydantic import BaseModel, Field

class Settings(BaseModel):
    ollama_host: str = Field(
        default_factory=lambda: os.getenv("OLLAMA_HOST", "127.0.0.1:11434")
    )
    model_name: str = Field(
        default_factory=lambda: os.getenv(
            "NIXPULSE_MODEL",
            "hf.co/unsloth/gemma-4-E2B-it-GGUF:Q4_K_M"
        )
    )
    session_name: str = Field(
        default_factory=lambda: os.getenv("NIXPULSE_SESSION", "nixpulse")
    )
    state_dir: str = Field(
        default_factory=lambda: os.getenv(
            "NIXPULSE_STATE_DIR",
            os.path.join(os.getenv("XDG_RUNTIME_DIR", "/tmp"), "nixpulse"),
        )
    )
    stderr_fifo: str = Field(
        default_factory=lambda: os.getenv(
            "NIXPULSE_STDERR_FIFO",
            os.path.join(
                os.getenv(
                    "NIXPULSE_STATE_DIR",
                    os.path.join(os.getenv("XDG_RUNTIME_DIR", "/tmp"), "nixpulse"),
                ),
                "stderr.pipe",
            ),
        )
    )
    stderr_log: str = Field(
        default_factory=lambda: os.path.join(
            os.getenv(
                "NIXPULSE_STATE_DIR",
                os.path.join(os.getenv("XDG_RUNTIME_DIR", "/tmp"), "nixpulse"),
            ),
            "stderr.log",
        )
    )

settings = Settings()
