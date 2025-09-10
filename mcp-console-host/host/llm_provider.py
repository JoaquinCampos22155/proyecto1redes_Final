# llm_provider.py
import os
import anthropic

class AnthropicProvider:
    def __init__(self, api_key: str | None = None, model: str | None = None, max_tokens: int = 512):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")
        self.client = anthropic.Anthropic(api_key=self.api_key)
        # Usa la de tu .env o cae en un default válido
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")
        self.max_tokens = max_tokens

    def chat(self, history: list[dict[str, str]]) -> tuple[str, None]:
        """
        history: lista de turnos {"role": "user"|"assistant", "content": "texto"}
        return: (respuesta_texto, tool_intent=None)  # por ahora sin tools
        """
        # Anthropic espera turns alternando "user" / "assistant"
        msgs = [{"role": ("user" if h["role"] == "user" else "assistant"), "content": h["content"]} for h in history]

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=msgs,
        )
        # resp.content es una lista de bloques; concatenamos los de texto
        text = ""
        for block in getattr(resp, "content", []) or []:
            # compatibilidad defensiva
            t = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if t == "text":
                text += getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "") or ""
        return text, None
