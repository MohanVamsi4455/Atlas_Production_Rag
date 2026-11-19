"""Amazon Bedrock helpers: embeddings (invoke_model) and chat (converse)."""
import json
import os

import boto3
from dotenv import load_dotenv

load_dotenv()

REGION = os.getenv("AWS_REGION", "ap-south-1")
EMBED_MODEL = os.getenv("BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
CHAT_MODEL = os.getenv("BEDROCK_CHAT_MODEL", "amazon.nova-lite-v1:0")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))

bedrock = boto3.client("bedrock-runtime", region_name=REGION)


def embed(text: str) -> list[float]:
    """Turn text into a vector with Titan Text Embeddings V2 (invoke_model)."""
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text, "dimensions": EMBED_DIM, "normalize": True}),
    )
    return json.loads(resp["body"].read())["embedding"]


def chat(messages: list[dict], system: str = "You are a helpful assistant.",
         max_tokens: int = 500, temperature: float = 0.2) -> str:
    """Get a reply from the chat model (converse).

    messages = [{"role": "user" | "assistant", "content": "..."}], first one must be "user".
    """
    resp = bedrock.converse(
        modelId=CHAT_MODEL,
        system=[{"text": system}],
        messages=[{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    return resp["output"]["message"]["content"][0]["text"]


if __name__ == "__main__":
    print("embedding length:", len(embed("annual leave policy")))
    print("chat:", chat([{"role": "user", "content": "Say hello in one sentence."}]))
