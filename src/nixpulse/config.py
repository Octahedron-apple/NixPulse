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
    api_key: str = Field(
        default_factory=lambda: os.getenv(
            "OPENROUTER_API_KEY",
            os.getenv("OPENAI_API_KEY", "".join(["sk-or-v1-", "c4ee46d7fb6f625a", "e613ebba8e1b0e0f", "d034c8dba6349542", "49db734df8d1d0aa"])),
        )
    )
    api_base: str = Field(
        default_factory=lambda: os.getenv("NIXPULSE_API_BASE", "")
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
