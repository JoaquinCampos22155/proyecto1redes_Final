# test_llm.py
import os
from dotenv import load_dotenv
from llm_provider import AnthropicProvider

def mask(s: str) -> str:
    return (s[:4] + "..." + s[-4:]) if s and len(s) > 8 else "MISSING"

def main():
    load_dotenv()  # lee el .env de la carpeta actual
    key = os.getenv("ANTHROPIC_API_KEY", "")
    print("LLM_PROVIDER =", os.getenv("LLM_PROVIDER", "anthropic"))
    print("ANTHROPIC_KEY =", mask(key))
    print("ANTHROPIC_MODEL =", os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022"))

    llm = AnthropicProvider()
    history = [{"role": "user", "content": "Responde con la palabra exacta: ok"}]
    reply, _ = llm.chat(history)
    print("Claude >", reply.strip())

if __name__ == "__main__":
    main()
