"""
Tencent Cloud TMT provider.
"""
from app.core.config import settings

try:
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.tmt.v20180321 import models, tmt_client
except ImportError:  # pragma: no cover - handled at runtime if dependency is absent
    credential = None
    ClientProfile = None
    HttpProfile = None
    models = None
    tmt_client = None


class TencentTranslator:
    provider = "tencent"

    def __init__(
        self,
        secret_id: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
    ) -> None:
        self.secret_id = (secret_id if secret_id is not None else settings.tencent_secret_id).strip()
        self.secret_key = (secret_key if secret_key is not None else settings.tencent_secret_key).strip()
        self.region = (
            region if region is not None else settings.tencent_tmt_region
        ).strip() or "ap-guangzhou"

    def translate_to_zh(self, text: str) -> str:
        source_text = str(text or "").strip()
        if not source_text:
            raise ValueError("text is empty")

        if not self.secret_id or not self.secret_key:
            raise RuntimeError("TENCENT_SECRET_ID / TENCENT_SECRET_KEY is not configured")

        if any(item is None for item in (credential, ClientProfile, HttpProfile, models, tmt_client)):
            raise RuntimeError("tencentcloud-sdk-python package is not installed")

        cred = credential.Credential(self.secret_id, self.secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = "tmt.tencentcloudapi.com"

        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile

        client = tmt_client.TmtClient(cred, self.region, client_profile)
        request = models.TextTranslateRequest()
        request.SourceText = source_text
        request.Source = "en"
        request.Target = "zh"
        request.ProjectId = 0

        response = client.TextTranslate(request)
        translated_text = str(getattr(response, "TargetText", "") or "").strip()
        if not translated_text:
            raise RuntimeError("Tencent TMT returned an empty translation")

        return translated_text
