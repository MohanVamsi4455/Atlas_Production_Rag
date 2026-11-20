from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(',') if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    # AWS
    aws_region: str = 'ap-south-1'
    aws_access_key_id: str = ''
    aws_secret_access_key: str = ''
    s3_bucket: str = ''

    # Chat providers (comma-separated; tried in order, next one is the failover)
    chat_provider_order: str = 'groq,gemini'

    # Gemini (Google AI Studio)
    gemini_api_key: str = ''
    gemini_chat_models: str = ''
    gemini_embed_model: str = 'gemini-embedding-001'
    embed_dim: int = 1024

    # Groq Cloud (chat only, no embeddings)
    groq_api_key: str = ''
    groq_chat_models: str = ''

    # Pinecone
    pinecone_api_key: str = ''
    pinecone_index_name: str = 'atlas-enterprise-ai-index'
    pinecone_cloud: str = 'aws'
    pinecone_region: str = 'us-east-1'
    pinecone_ready_timeout: int = 180

    # PostgreSQL (Render External Database URL)
    database_url: str = ''

    # Rius (optional)
    rius_api_key: str = ''
    rius_service_name: str = 'atlas-ai-enterprise'
    rius_disabled: bool = False
    rius_capture_content: bool = False

    # App behaviour
    max_upload_bytes: int = 10485760
    min_relevance: float = 0.25
    hitl_enabled: bool = True
    pii_mode: str = 'redact'  # 'redact' = block IDs in questions, mask PII in evidence/answers; 'off' = disabled
    memory_turns: int = 6

    @property
    def chat_providers(self) -> list[str]:
        return _split(self.chat_provider_order)

    @property
    def gemini_models(self) -> list[str]:
        return _split(self.gemini_chat_models)

    @property
    def groq_models(self) -> list[str]:
        return _split(self.groq_chat_models)


@lru_cache
def settings() -> Settings:
    return Settings()
