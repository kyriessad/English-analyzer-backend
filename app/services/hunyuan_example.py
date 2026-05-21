"""
Use Tencent Hunyuan LLM to generate an English example sentence for a word or phrase.
Failure is always silent and never affects the main analysis flow.
"""
import json

from app.core.config import settings


def generate_example_with_hunyuan(
    text: str,
    chinese_meaning: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Call Tencent Hunyuan ChatCompletions to generate an example sentence.

    Returns (exampleSentence, exampleTranslation) or (None, None) on any failure,
    including missing credentials, service not activated, or validation mismatch.
    """
    try:
        from tencentcloud.common import credential as tencent_credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.hunyuan.v20230901 import hunyuan_client
        from tencentcloud.hunyuan.v20230901 import models as hunyuan_models

        secret_id = (settings.tencent_secret_id or "").strip()
        secret_key = (settings.tencent_secret_key or "").strip()
        if not secret_id or not secret_key:
            return None, None

        cred = tencent_credential.Credential(secret_id, secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = "hunyuan.tencentcloudapi.com"
        http_profile.reqTimeout = 12
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        client = hunyuan_client.HunyuanClient(cred, "", client_profile)

        meaning_hint = f"（中文含义：{chinese_meaning}）" if chinese_meaning else ""
        prompt = (
            f'请为英文单词或短语"{text}"{meaning_hint}生成一个自然的英文例句，并提供该例句的中文翻译。\n'
            "要求：\n"
            f'1. exampleSentence 必须是完整英文句子，且必须自然地包含"{text}"或其变形（过去式、进行时等均可）\n'
            "2. 例句要简洁、日常、生动易懂，不少于 4 个英文单词\n"
            "3. exampleTranslation 是该例句的中文翻译\n"
            "4. 仅返回以下 JSON 格式，不要有任何其他内容或 Markdown：\n"
            '{"exampleSentence": "...", "exampleTranslation": "..."}'
        )

        msg = hunyuan_models.Message()
        msg.Role = "user"
        msg.Content = prompt

        req = hunyuan_models.ChatCompletionsRequest()
        req.Model = "hunyuan-lite"
        req.Messages = [msg]
        req.Stream = False

        resp = client.ChatCompletions(req)
        content = str(resp.Choices[0].Message.Content or "").strip()

        # Extract JSON — guard against leading/trailing prose from the model
        start = content.find("{")
        end = content.rfind("}") + 1
        if start == -1 or end <= start:
            return None, None

        data = json.loads(content[start:end])
        sentence = str(data.get("exampleSentence") or "").strip()
        translation = str(data.get("exampleTranslation") or "").strip()

        # Both fields must be present
        if not sentence or not translation:
            return None, None
        # Must not be the bare input text itself
        if sentence.lower().strip() == text.lower().strip():
            return None, None
        # Must contain the original input (substring, handles inflections like craved/craving)
        if text.lower() not in sentence.lower():
            return None, None
        # Must be a real sentence, not a single word
        if len(sentence.split()) < 3:
            return None, None

        return sentence, translation
    except Exception:
        return None, None
