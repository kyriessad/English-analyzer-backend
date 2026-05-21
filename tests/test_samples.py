import csv
import os
from pathlib import Path
from typing import Any

import requests


DEFAULT_API_URL = "http://127.0.0.1:8000/api/analyze-english"
API_URL = os.getenv("ANALYZER_API_URL", DEFAULT_API_URL).strip() or DEFAULT_API_URL
REQUEST_TIMEOUT_SECONDS = 30
MARKDOWN_TRANSLATION_MAX_LENGTH = 80

BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "test_results"
CSV_PATH = RESULT_DIR / "analyze_english_results.csv"
MD_PATH = RESULT_DIR / "analyze_english_results.md"


TEST_SAMPLES = [
    {"id": 1, "text": "apply", "expected_level": "pass", "expected_category": "word", "note": "正常单词"},
    {"id": 2, "text": "improve", "expected_level": "pass", "expected_category": "word", "note": "正常单词"},
    {"id": 3, "text": "vivid", "expected_level": "pass", "expected_category": "word", "note": "正常单词"},
    {"id": 4, "text": "beautiful", "expected_level": "pass", "expected_category": "word", "note": "正常单词"},
    {"id": 5, "text": "environment", "expected_level": "pass", "expected_category": "word", "note": "正常单词"},
    {"id": 6, "text": "responsibility", "expected_level": "pass", "expected_category": "word", "note": "正常单词"},
    {"id": 7, "text": "opportunity", "expected_level": "pass", "expected_category": "word", "note": "正常单词"},
    {"id": 8, "text": "significant", "expected_level": "pass", "expected_category": "word", "note": "正常单词"},
    {"id": 9, "text": "challenge", "expected_level": "pass", "expected_category": "word", "note": "正常单词"},
    {"id": 10, "text": "knowledge", "expected_level": "pass", "expected_category": "word", "note": "正常单词"},
    {"id": 11, "text": "autum", "expected_level": "warning", "expected_category": "word", "note": "拼写弱提醒，可能提示 autumn"},
    {"id": 12, "text": "enviroment", "expected_level": "warning", "expected_category": "word", "note": "拼写弱提醒，可能提示 environment"},
    {"id": 13, "text": "recieve", "expected_level": "warning", "expected_category": "word", "note": "拼写弱提醒，可能提示 receive"},
    {"id": 14, "text": "becuase", "expected_level": "warning", "expected_category": "word", "note": "拼写弱提醒，可能提示 because"},
    {"id": 15, "text": "definately", "expected_level": "warning", "expected_category": "word", "note": "拼写弱提醒，可能提示 definitely"},
    {"id": 16, "text": "goverment", "expected_level": "warning", "expected_category": "word", "note": "拼写弱提醒，可能提示 government"},
    {"id": 17, "text": "seperate", "expected_level": "warning", "expected_category": "word", "note": "拼写弱提醒，可能提示 separate"},
    {"id": 18, "text": "acheive", "expected_level": "warning", "expected_category": "word", "note": "拼写弱提醒，可能提示 achieve"},
    {"id": 19, "text": "frend", "expected_level": "warning", "expected_category": "word", "note": "拼写弱提醒，可能提示 friend"},
    {"id": 20, "text": "happyness", "expected_level": "warning", "expected_category": "word", "note": "拼写弱提醒，可能提示 happiness"},
    {"id": 21, "text": "ChatGPT", "expected_level": "pass", "expected_category": "word", "note": "专有名词，不应拼写误判"},
    {"id": 22, "text": "OpenAI", "expected_level": "pass", "expected_category": "word", "note": "专有名词，不应拼写误判"},
    {"id": 23, "text": "iPhone", "expected_level": "pass", "expected_category": "word", "note": "混合大小写，不应误判"},
    {"id": 24, "text": "YouTube", "expected_level": "pass", "expected_category": "word", "note": "专有名词"},
    {"id": 25, "text": "TikTok", "expected_level": "pass", "expected_category": "word", "note": "专有名词"},
    {"id": 26, "text": "DeepL", "expected_level": "pass", "expected_category": "word", "note": "专有名词"},
    {"id": 27, "text": "Google", "expected_level": "pass", "expected_category": "word", "note": "首字母大写，不应误判"},
    {"id": 28, "text": "Microsoft", "expected_level": "pass", "expected_category": "word", "note": "首字母大写，不应误判"},
    {"id": 29, "text": "Lebron", "expected_level": "pass", "expected_category": "word", "note": "人名，不应 error"},
    {"id": 30, "text": "London", "expected_level": "pass", "expected_category": "word", "note": "地名，不应 error"},
    {"id": 31, "text": "API", "expected_level": "pass", "expected_category": "word", "note": "全大写缩写，不应拼写检查"},
    {"id": 32, "text": "IELTS", "expected_level": "pass", "expected_category": "word", "note": "全大写缩写"},
    {"id": 33, "text": "TOEFL", "expected_level": "pass", "expected_category": "word", "note": "全大写缩写"},
    {"id": 34, "text": "GDP", "expected_level": "pass", "expected_category": "word", "note": "全大写缩写"},
    {"id": 35, "text": "AI", "expected_level": "pass", "expected_category": "word", "note": "全大写缩写"},
    {"id": 36, "text": "women's", "expected_level": "pass", "expected_category": "word", "note": "所有格，不应 error"},
    {"id": 37, "text": "children's", "expected_level": "pass", "expected_category": "word", "note": "所有格"},
    {"id": 38, "text": "teacher's", "expected_level": "pass", "expected_category": "word", "note": "所有格"},
    {"id": 39, "text": "don't", "expected_level": "pass", "expected_category": "word", "note": "缩写，不应 error"},
    {"id": 40, "text": "can't", "expected_level": "pass", "expected_category": "word", "note": "缩写，不应 error"},
    {"id": 41, "text": "GPT-4", "expected_level": "pass", "expected_category": "unknown", "note": "含符号/数字，不应 error"},
    {"id": 42, "text": "B2B", "expected_level": "pass", "expected_category": "unknown", "note": "含数字术语，不应 error"},
    {"id": 43, "text": "P2P", "expected_level": "pass", "expected_category": "unknown", "note": "含数字术语，不应 error"},
    {"id": 44, "text": "COVID-19", "expected_level": "pass", "expected_category": "unknown", "note": "含数字/连字符术语"},
    {"id": 45, "text": "e-mail", "expected_level": "pass", "expected_category": "word", "note": "连字符词，Phase 8H 修复后应判 word"},
    {"id": 46, "text": "look forward to", "expected_level": "pass", "expected_category": "phrase", "note": "短语"},
    {"id": 47, "text": "give up", "expected_level": "pass", "expected_category": "phrase", "note": "短语"},
    {"id": 48, "text": "take notes", "expected_level": "pass", "expected_category": "phrase", "note": "短语"},
    {"id": 49, "text": "on the same page", "expected_level": "pass", "expected_category": "phrase", "note": "短语"},
    {"id": 50, "text": "break down", "expected_level": "pass", "expected_category": "phrase", "note": "短语"},
    {"id": 51, "text": "pay attention to", "expected_level": "pass", "expected_category": "phrase", "note": "短语"},
    {"id": 52, "text": "make a difference", "expected_level": "pass", "expected_category": "phrase", "note": "短语"},
    {"id": 53, "text": "in terms of", "expected_level": "pass", "expected_category": "phrase", "note": "短语"},
    {"id": 54, "text": "as a result", "expected_level": "pass", "expected_category": "phrase", "note": "短语"},
    {"id": 55, "text": "due to", "expected_level": "pass", "expected_category": "phrase", "note": "短语"},
    {"id": 56, "text": "Go away.", "expected_level": "pass", "expected_category": "sentence", "note": "短句，因句号应判 sentence"},
    {"id": 57, "text": "I love you.", "expected_level": "pass", "expected_category": "sentence", "note": "3 个词但有句号，应判 sentence"},
    {"id": 58, "text": "Stop it!", "expected_level": "pass", "expected_category": "sentence", "note": "感叹号 sentence"},
    {"id": 59, "text": "Are you sure?", "expected_level": "pass", "expected_category": "sentence", "note": "问号 sentence"},
    {"id": 60, "text": "I am fine.", "expected_level": "pass", "expected_category": "sentence", "note": "短句 sentence"},
    {"id": 61, "text": "This is a book.", "expected_level": "pass", "expected_category": "sentence", "note": "普通句子"},
    {"id": 62, "text": "I have been working on this project.", "expected_level": "pass", "expected_category": "sentence", "note": "普通句子"},
    {"id": 63, "text": "Practice makes perfect.", "expected_level": "pass", "expected_category": "sentence", "note": "普通句子"},
    {"id": 64, "text": "Time is money.", "expected_level": "pass", "expected_category": "sentence", "note": "普通句子"},
    {"id": 65, "text": "Never give up.", "expected_level": "pass", "expected_category": "sentence", "note": "短句 sentence"},
    {"id": 66, "text": "I want to improve my English every day", "expected_level": "pass", "expected_category": "sentence", "note": "6 个以上词，无标点也应 sentence"},
    {"id": 67, "text": "Reading books can help us understand the world", "expected_level": "pass", "expected_category": "sentence", "note": "6 个以上词"},
    {"id": 68, "text": "This method is useful for solving difficult problems", "expected_level": "pass", "expected_category": "sentence", "note": "6 个以上词"},
    {"id": 69, "text": "Students should learn how to manage their time", "expected_level": "pass", "expected_category": "sentence", "note": "6 个以上词"},
    {"id": 70, "text": "The result shows that this approach is effective", "expected_level": "pass", "expected_category": "sentence", "note": "6 个以上词"},
    {"id": 71, "text": "hello 世界", "expected_level": "warning", "expected_category": "phrase/unknown", "note": "中英混合 warning"},
    {"id": 72, "text": "apply 申请", "expected_level": "warning", "expected_category": "phrase/unknown", "note": "中英混合 warning"},
    {"id": 73, "text": "ChatGPT 是 useful", "expected_level": "warning", "expected_category": "sentence/unknown", "note": "中英混合 warning"},
    {"id": 74, "text": "I love 你", "expected_level": "warning", "expected_category": "phrase/unknown", "note": "中英混合 warning"},
    {"id": 75, "text": "good morning 早上好", "expected_level": "warning", "expected_category": "phrase/unknown", "note": "中英混合 warning"},
    {"id": 76, "text": "你好", "expected_level": "error", "expected_category": "unknown", "note": "纯中文 error"},
    {"id": 77, "text": "世界", "expected_level": "error", "expected_category": "unknown", "note": "纯中文 error"},
    {"id": 78, "text": "申请", "expected_level": "error", "expected_category": "unknown", "note": "纯中文 error"},
    {"id": 79, "text": "我爱你", "expected_level": "error", "expected_category": "unknown", "note": "纯中文 error"},
    {"id": 80, "text": "这是一个句子", "expected_level": "error", "expected_category": "unknown", "note": "纯中文 error"},
    {"id": 81, "text": "123456", "expected_level": "error", "expected_category": "unknown", "note": "纯数字 error"},
    {"id": 82, "text": "2026", "expected_level": "error", "expected_category": "unknown", "note": "纯数字 error"},
    {"id": 83, "text": "3.14159", "expected_level": "error", "expected_category": "unknown", "note": "纯数字/符号 error"},
    {"id": 84, "text": "@@@@@", "expected_level": "error", "expected_category": "unknown", "note": "纯符号 error"},
    {"id": 85, "text": "!!!!!!", "expected_level": "error", "expected_category": "unknown", "note": "纯符号 error"},
    {"id": 86, "text": "......", "expected_level": "error", "expected_category": "unknown", "note": "纯符号 error"},
    {"id": 87, "text": "#$%^&*", "expected_level": "error", "expected_category": "unknown", "note": "纯符号 error"},
    {"id": 88, "text": "abc123", "expected_level": "pass", "expected_category": "unknown", "note": "字母+数字，不应 error"},
    {"id": 89, "text": "hello!!!", "expected_level": "warning", "expected_category": "sentence/unknown", "note": "有英文，不应 error"},
    {"id": 90, "text": "apply???", "expected_level": "warning", "expected_category": "sentence", "note": "问号结尾，应 sentence"},
    {"id": 91, "text": "a", "expected_level": "pass/warning", "expected_category": "word", "note": "单字母，观察是否误报"},
    {"id": 92, "text": "I", "expected_level": "pass", "expected_category": "word", "note": "大写单词，不应拼写误判"},
    {"id": 93, "text": "am", "expected_level": "pass", "expected_category": "word", "note": "常见词"},
    {"id": 94, "text": "the", "expected_level": "pass", "expected_category": "word", "note": "常见词"},
    {"id": 95, "text": "to", "expected_level": "pass", "expected_category": "word", "note": "常见连接词"},
    {"id": 96, "text": "I am on the same page with you.", "expected_level": "pass", "expected_category": "sentence", "note": "句子，翻译是否自然"},
    {"id": 97, "text": "It is important to develop a good habit of reviewing English words regularly.", "expected_level": "pass", "expected_category": "sentence", "note": "较长句子"},
    {"id": 98, "text": "Although the problem looks simple, it actually requires careful analysis and repeated practice.", "expected_level": "pass", "expected_category": "sentence", "note": "复杂句"},
    {"id": 99, "text": "Learning English is not only about memorizing words but also about understanding how they are used in real contexts.", "expected_level": "pass", "expected_category": "sentence", "note": "长句翻译"},
    {"id": 100, "text": "This passage is designed to test whether the backend can correctly identify a relatively long English paragraph and give a gentle warning instead of blocking the user from saving the card, because the user may still want to keep this content for later review.", "expected_level": "warning", "expected_category": "paragraph", "note": "长段落，应 warning，不应 error"},
    {"id": 101, "text": "I like apples, bananas, and oranges.", "expected_level": "pass", "expected_category": "sentence", "note": "标准逗号用法"},
    {"id": 102, "text": "I like apples,bananas, and oranges.", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "I like apples, bananas, and oranges.", "note": "逗号后缺空格"},
    {"id": 103, "text": "I like apples,  bananas, and oranges.", "expected_level": "pass", "expected_category": "sentence", "note": "逗号后多个空格"},
    {"id": 104, "text": "I like apples , bananas, and oranges.", "expected_level": "pass", "expected_category": "sentence", "note": "逗号前多空格"},
    {"id": 105, "text": "I like apples ,bananas, and oranges.", "expected_level": "pass", "expected_category": "sentence", "note": "逗号前多空格且逗号后缺空格"},
    {"id": 106, "text": "I like apples, bananas,and oranges.", "expected_level": "pass", "expected_category": "sentence", "note": "第二个逗号后缺空格"},
    {"id": 107, "text": "I like apples ,  bananas ,  and oranges.", "expected_level": "pass", "expected_category": "sentence", "note": "多处逗号空格不规范"},
    {"id": 108, "text": "This is good.But I need more practice.", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "This is good. But I need more practice.", "note": "句号后缺空格"},
    {"id": 109, "text": "This is good. But I need more practice.", "expected_level": "pass", "expected_category": "sentence", "note": "句号后正常空格"},
    {"id": 110, "text": "This is good . But I need more practice.", "expected_level": "pass", "expected_category": "sentence", "note": "句号前多空格"},
    {"id": 111, "text": "Are you sure?Yes, I am.", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "Are you sure? Yes, I am.", "note": "问号后缺空格"},
    {"id": 112, "text": "Are you sure? Yes, I am.", "expected_level": "pass", "expected_category": "sentence", "note": "问号后正常空格"},
    {"id": 113, "text": "Are you sure ? Yes, I am.", "expected_level": "pass", "expected_category": "sentence", "note": "问号前多空格"},
    {"id": 114, "text": "Stop it!Now!", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "Stop it! Now!", "note": "感叹号后缺空格"},
    {"id": 115, "text": "Stop it! Now!", "expected_level": "pass", "expected_category": "sentence", "note": "感叹号后正常空格"},
    {"id": 116, "text": "Stop it ! Now!", "expected_level": "pass", "expected_category": "sentence", "note": "感叹号前多空格"},
    {"id": 117, "text": "I really like this book...", "expected_level": "warning", "expected_category": "sentence", "note": "连续省略点，允许但提醒"},
    {"id": 118, "text": "I really like this book!!!", "expected_level": "warning", "expected_category": "sentence", "note": "连续感叹号，提醒"},
    {"id": 119, "text": "What are you doing???", "expected_level": "warning", "expected_category": "sentence", "note": "连续问号，提醒"},
    {"id": 120, "text": "This is amazing?!", "expected_level": "warning", "expected_category": "sentence", "note": "混合标点，提醒"},
    {"id": 121, "text": "This is a well-known fact.", "expected_level": "pass", "expected_category": "sentence", "note": "合法连字符复合词"},
    {"id": 122, "text": "This is a well - known fact.", "expected_level": "pass", "expected_category": "sentence", "note": "连字符两侧多空格"},
    {"id": 123, "text": "This is a well- known fact.", "expected_level": "pass", "expected_category": "sentence", "note": "连字符后多空格"},
    {"id": 124, "text": "This is a well -known fact.", "expected_level": "pass", "expected_category": "sentence", "note": "连字符前多空格"},
    {"id": 125, "text": "We need a long-term plan.", "expected_level": "pass", "expected_category": "sentence", "note": "合法连字符短语"},
    {"id": 126, "text": "We need a long--term plan.", "expected_level": "pass", "expected_category": "sentence", "note": "连续连字符异常"},
    {"id": 127, "text": "We need a long---term plan.", "expected_level": "pass", "expected_category": "sentence", "note": "多重连字符异常"},
    {"id": 128, "text": "This method is useful - but not perfect.", "expected_level": "pass", "expected_category": "sentence", "note": "普通 hyphen 当破折号，空格格式不理想"},
    {"id": 129, "text": "This method is useful—but not perfect.", "expected_level": "pass", "expected_category": "sentence", "note": "em dash 可接受"},
    {"id": 130, "text": "This method is useful — but not perfect.", "expected_level": "pass", "expected_category": "sentence", "note": "em dash 两侧空格可接受"},
    {"id": 131, "text": "I don't know what you're talking about.", "expected_level": "pass", "expected_category": "sentence", "note": "缩写和撇号"},
    {"id": 132, "text": "I don 't know what you 're talking about.", "expected_level": "pass", "expected_category": "sentence", "note": "撇号前后错误空格"},
    {"id": 133, "text": "The student's answer is correct.", "expected_level": "pass", "expected_category": "sentence", "note": "所有格"},
    {"id": 134, "text": "The student 's answer is correct.", "expected_level": "pass", "expected_category": "sentence", "note": "所有格撇号前多空格"},
    {"id": 135, "text": "He said, \"I will come back soon.\"", "expected_level": "pass", "expected_category": "sentence", "note": "英文引号"},
    {"id": 136, "text": "He said ,\"I will come back soon.\"", "expected_level": "pass", "expected_category": "sentence", "note": "逗号前多空格、逗号后缺空格"},
    {"id": 137, "text": "He said, “I will come back soon.”", "expected_level": "pass", "expected_category": "sentence", "note": "中文/弯引号可接受"},
    {"id": 138, "text": "He said，“I will come back soon.”", "expected_level": "pass", "expected_category": "sentence", "note": "中文逗号混入英文句"},
    {"id": 139, "text": "I bought apples，bananas，and oranges.", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "I bought apples, bananas, and oranges.", "note": "中文逗号混入英文句"},
    {"id": 140, "text": "I bought apples, bananas，and oranges.", "expected_level": "pass", "expected_category": "sentence", "note": "中英文逗号混用"},
    {"id": 141, "text": "This is important。Please remember it.", "expected_level": "pass", "expected_category": "sentence", "note": "中文句号混入英文句"},
    {"id": 142, "text": "This is important. Please remember it.", "expected_level": "pass", "expected_category": "sentence", "note": "标准两句"},
    {"id": 143, "text": "This is (very) important.", "expected_level": "pass", "expected_category": "sentence", "note": "括号正常"},
    {"id": 144, "text": "This is(very)important.", "expected_level": "pass", "expected_category": "sentence", "note": "括号前后缺空格"},
    {"id": 145, "text": "This is ( very ) important.", "expected_level": "pass", "expected_category": "sentence", "note": "括号内部多余空格"},
    {"id": 146, "text": "The price increased by 12.5% last year.", "expected_level": "pass", "expected_category": "sentence", "note": "英文句子中含数值百分比"},
    {"id": 147, "text": "The meeting is on 2026-05-05.", "expected_level": "pass", "expected_category": "sentence", "note": "英文句子中含日期"},
    {"id": 148, "text": "I have 3.14159 reasons to doubt this result.", "expected_level": "pass", "expected_category": "sentence", "note": "英文句子中含小数，不应 error"},
    {"id": 149, "text": "This sentencehas a missing space.", "expected_level": "pass", "expected_category": "sentence", "note": "明显缺空格，sentencehas 可疑"},
    {"id": 150, "text": "This sentence has     too many spaces between words.", "expected_level": "pass", "expected_category": "sentence", "note": "单词间多个空格"},
    {"id": 151, "text": "Hello！How are you？", "expected_level": "pass", "expected_category": "sentence", "note": "中文感叹号和问号 auto-fix"},
    {"id": 152, "text": "Are you sure？Yes, I am.", "expected_level": "pass", "expected_category": "sentence", "note": "中文问号 auto-fix"},
    {"id": 153, "text": "Stop it！Now！", "expected_level": "pass", "expected_category": "sentence", "note": "中文感叹号 auto-fix"},
    {"id": 154, "text": "This is（very）important.", "expected_level": "pass", "expected_category": "sentence", "note": "中文括号 auto-fix"},
    {"id": 155, "text": "This is “important”.", "expected_level": "pass", "expected_category": "sentence", "note": "弯引号 auto-fix"},
    {"id": 156, "text": "I don‘t know.", "expected_level": "pass", "expected_category": "sentence", "note": "中文撇号 auto-fix"},
    {"id": 157, "text": "The student‘s answer is correct.", "expected_level": "pass", "expected_category": "sentence", "note": "所有格中文撇号 auto-fix"},
    {"id": 158, "text": "I don 't know.", "expected_level": "pass", "expected_category": "sentence", "note": "撇号空格 auto-fix"},
    {"id": 159, "text": "The students ' books are on the desk.", "expected_level": "pass", "expected_category": "sentence", "note": "复数所有格撇号空格 auto-fix"},
    {"id": 160, "text": "hello   world", "expected_level": "pass", "expected_category": "phrase", "note": "多个空格 auto-fix"},
    {"id": 161, "text": "I like apples , bananas.", "expected_level": "pass", "expected_category": "sentence", "note": "标点前空格 auto-fix"},
    {"id": 162, "text": "This is good .", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "This is good.", "note": "句号前空格 auto-fix"},
    {"id": 163, "text": "state - of - the - art", "expected_level": "pass", "expected_category": "phrase", "note": "连字符空格放过"},
    {"id": 164, "text": "I don't know.", "expected_level": "pass", "expected_category": "sentence", "note": "正常短句不应 warning"},
    {"id": 165, "text": "I see.", "expected_level": "pass", "expected_category": "sentence", "note": "正常短句不应 warning"},
    {"id": 166, "text": "I agree.", "expected_level": "pass", "expected_category": "sentence", "note": "正常短句不应 warning"},
    {"id": 167, "text": "Really!?", "expected_level": "warning", "expected_category": "sentence", "note": "混合标点 warning"},
    {"id": 168, "text": "Wait.....", "expected_level": "warning", "expected_category": "sentence", "note": "连续句点 warning"},
    {"id": 169, "text": "hello！！！", "expected_level": "warning", "expected_category": "sentence", "note": "中文感叹号 auto-fix 后连续标点 warning"},
    {"id": 170, "text": "你好。", "expected_level": "error", "expected_category": "unknown", "note": "纯中文带中文句号 error"},
    {"id": 171, "text": "我爱你！", "expected_level": "error", "expected_category": "unknown", "note": "纯中文带中文感叹号 error"},
    {"id": 172, "text": "12.5%", "expected_level": "error", "expected_category": "unknown", "note": "纯数值百分比 error"},
    {"id": 173, "text": "2026-05-05", "expected_level": "error", "expected_category": "unknown", "note": "纯日期数值 error"},
    {"id": 174, "text": "10/20", "expected_level": "error", "expected_category": "unknown", "note": "纯数值斜杠 error"},
    {"id": 175, "text": "1:30", "expected_level": "error", "expected_category": "unknown", "note": "纯时间数值 error"},
    {"id": 176, "text": "A4 paper", "expected_level": "pass", "expected_category": "phrase", "note": "英文内容中含数字不误伤"},
    {"id": 177, "text": "I have 3 apples.", "expected_level": "pass", "expected_category": "sentence", "note": "英文句子中含数字不误伤"},
    {"id": 178, "text": "The price is 12.5%.", "expected_level": "pass", "expected_category": "sentence", "note": "英文句子中含百分比不误伤"},
    {"id": 179, "text": "？？？？", "expected_level": "error", "expected_category": "unknown", "note": "纯中文问号 auto-fix 后纯符号 error"},
    {"id": 180, "text": "，，，，", "expected_level": "error", "expected_category": "unknown", "note": "纯中文逗号 auto-fix 后纯符号 error"},
    {"id": 181, "text": "This is good..", "expected_level": "pass", "expected_category": "sentence", "note": "两个句号 auto-fix"},
    {"id": 182, "text": "I like apples,, bananas.", "expected_level": "pass", "expected_category": "sentence", "note": "多个逗号 auto-fix"},
    {"id": 183, "text": "ＡＢＣ１２３", "expected_level": "pass", "expected_category": "unknown", "note": "全角英文字母和数字 auto-fix"},
    {"id": 184, "text": "I love　English.", "expected_level": "pass", "expected_category": "sentence", "note": "全角空格 auto-fix"},
    {"id": 185, "text": "I\nlove\nEnglish.", "expected_level": "pass", "expected_category": "sentence", "note": "换行 auto-fix"},
    {"id": 186, "text": "No problem.", "expected_level": "pass", "expected_category": "sentence", "note": "正常短句不应 warning"},
    {"id": 187, "text": "Thank you.", "expected_level": "pass", "expected_category": "sentence", "note": "正常短句不应 warning"},
    {"id": 188, "text": "You're welcome.", "expected_level": "pass", "expected_category": "sentence", "note": "正常短句不应 warning"},
    {"id": 189, "text": "he 'll be back.", "expected_level": "pass", "expected_category": "sentence", "note": "缩写撇号空格 auto-fix"},
    {"id": 190, "text": "we 've done it.", "expected_level": "pass", "expected_category": "sentence", "note": "缩写撇号空格 auto-fix"},
    {"id": 191, "text": "I bought apples， bananas， and oranges.", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "I bought apples, bananas, and oranges.", "note": "中文逗号和逗号后空格规范化"},
    {"id": 192, "text": "I have 3.14159 reasons.", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "I have 3.14159 reasons.", "note": "小数不应被空格规则破坏"},
    {"id": 193, "text": "The version is v1.2.3.", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "The version is v1.2.3.", "note": "版本号不应被空格规则破坏"},
    {"id": 194, "text": "U.S.A.", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "U.S.A.", "note": "缩写不应被空格规则破坏"},
    {"id": 195, "text": "Use e.g. examples carefully.", "expected_level": "pass", "expected_category": "sentence", "expected_normalizedText": "Use e.g. examples carefully.", "note": "e.g. 缩写不应被空格规则破坏"},
]


FIELDNAMES = [
    "id",
    "text",
    "expected_level",
    "actual_level",
    "level_pass",
    "expected_category",
    "actual_category",
    "category_pass",
    "expected_normalizedText",
    "actual_normalizedText",
    "normalizedText_pass",
    "ok",
    "translation",
    "warnings",
    "errors",
    "provider",
    "cacheHit",
    "note",
]


def expected_matches(expected: str, actual: str) -> bool:
    expected_values = [item.strip() for item in str(expected or "").split("/") if item.strip()]
    return str(actual or "") in expected_values


def join_messages(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, list):
        return " | ".join(str(item).replace("\r", " ").replace("\n", " ") for item in value)

    return str(value).replace("\r", " ").replace("\n", " ")


def call_analyzer(sample: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "text": sample["text"],
        "cardType": "auto",
        "targetLang": "zh",
    }

    try:
        response = requests.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
    except Exception as exc:
        return {
            "actual_level": "request_failed",
            "actual_category": "request_failed",
            "actual_normalizedText": "",
            "ok": False,
            "translation": None,
            "warnings": [],
            "errors": [str(exc)],
            "provider": None,
            "cacheHit": False,
        }

    return {
        "actual_level": data.get("level"),
        "actual_category": data.get("category"),
        "actual_normalizedText": data.get("normalizedText"),
        "ok": data.get("ok"),
        "translation": data.get("translation"),
        "warnings": data.get("warnings") or [],
        "errors": data.get("errors") or [],
        "provider": data.get("provider"),
        "cacheHit": data.get("cacheHit"),
    }


def build_result_row(sample: dict[str, Any]) -> dict[str, Any]:
    actual = call_analyzer(sample)
    level_pass = expected_matches(sample["expected_level"], actual["actual_level"])
    category_pass = expected_matches(sample["expected_category"], actual["actual_category"])
    expected_normalized_text = sample.get("expected_normalizedText")
    normalized_text_pass = (
        True
        if expected_normalized_text is None
        else actual["actual_normalizedText"] == expected_normalized_text
    )

    return {
        "id": sample["id"],
        "text": sample["text"],
        "expected_level": sample["expected_level"],
        "actual_level": actual["actual_level"],
        "level_pass": level_pass,
        "expected_category": sample["expected_category"],
        "actual_category": actual["actual_category"],
        "category_pass": category_pass,
        "expected_normalizedText": expected_normalized_text or "",
        "actual_normalizedText": actual["actual_normalizedText"] or "",
        "normalizedText_pass": normalized_text_pass,
        "ok": actual["ok"],
        "translation": actual["translation"] or "",
        "warnings": join_messages(actual["warnings"]),
        "errors": join_messages(actual["errors"]),
        "provider": actual["provider"],
        "cacheHit": actual["cacheHit"],
        "note": sample["note"],
    }


def truncate_markdown_translation(value: Any) -> str:
    text = str(value if value is not None else "")
    if len(text) <= MARKDOWN_TRANSLATION_MAX_LENGTH:
        return text
    return text[:MARKDOWN_TRANSLATION_MAX_LENGTH] + "..."


def markdown_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def write_csv(rows: list[dict[str, Any]]) -> None:
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Analyze English Test Results",
        "",
        f"- API URL: `{API_URL}`",
        f"- Total: {len(rows)}",
        "",
        "| " + " | ".join(FIELDNAMES) + " |",
        "| " + " | ".join(["---"] * len(FIELDNAMES)) + " |",
    ]

    for row in rows:
        markdown_row = dict(row)
        markdown_row["translation"] = truncate_markdown_translation(markdown_row.get("translation"))
        lines.append("| " + " | ".join(markdown_escape(markdown_row.get(field)) for field in FIELDNAMES) + " |")

    MD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary(rows: list[dict[str, Any]]) -> None:
    total = len(rows)
    level_pass_count = sum(1 for row in rows if row["level_pass"])
    category_pass_count = sum(1 for row in rows if row["category_pass"])
    all_pass_rows = [
        row
        for row in rows
        if row["level_pass"] and row["category_pass"] and row["normalizedText_pass"]
    ]
    failed_cases = [
        row["id"]
        for row in rows
        if not (row["level_pass"] and row["category_pass"] and row["normalizedText_pass"])
    ]

    print("summary:")
    print(f"  total: {total}")
    print(f"  level_pass_count: {level_pass_count}")
    print(f"  category_pass_count: {category_pass_count}")
    print(f"  all_pass_count: {len(all_pass_rows)}")
    print(f"  failed_cases: {failed_cases}")
    print(f"  csv: {CSV_PATH}")
    print(f"  markdown: {MD_PATH}")


def main() -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    for sample in TEST_SAMPLES:
        print(f"Testing {sample['id']}: {sample['text']!r}")
        rows.append(build_result_row(sample))

    write_csv(rows)
    write_markdown(rows)
    print_summary(rows)


if __name__ == "__main__":
    main()
