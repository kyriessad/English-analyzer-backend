"""
生成我的理解文案
"""
from app.schemas import Category


def generate_understanding(
    text: str,
    category: Category,
    translation: str | None,
) -> str:
    if not translation:
        return "你可以在这里写下自己对这个内容的理解。"

    if category == "word":
        return f"这个词可以理解为“{translation}”。建议结合例句记忆它的具体用法。"

    if category == "phrase":
        return f"这个短语大致表示“{translation}”。复习时可以重点看它在句子中的搭配方式。"

    if category == "sentence":
        return f"这句话可以理解为：“{translation}”。建议关注句子结构和常用表达。"

    return f"这个内容大致可以理解为：“{translation}”。"

