# Analyze English Test Results

- API URL: `http://127.0.0.1:8000/api/analyze-english`
- Total: 195

| id | text | expected_level | actual_level | level_pass | expected_category | actual_category | category_pass | expected_normalizedText | actual_normalizedText | normalizedText_pass | ok | translation | warnings | errors | provider | cacheHit | note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | apply | pass | pass | True | word | word | True |  | apply | True | True | 适用 |  |  | tencent | False | 正常单词 |
| 2 | improve | pass | pass | True | word | word | True |  | improve | True | True | 提高 |  |  | tencent | False | 正常单词 |
| 3 | vivid | pass | pass | True | word | word | True |  | vivid | True | True | 生动 |  |  | tencent | False | 正常单词 |
| 4 | beautiful | pass | pass | True | word | word | True |  | beautiful | True | True | 美丽 |  |  | tencent | False | 正常单词 |
| 5 | environment | pass | pass | True | word | word | True |  | environment | True | True | 环境 |  |  | tencent | False | 正常单词 |
| 6 | responsibility | pass | pass | True | word | word | True |  | responsibility | True | True | 责任 |  |  | tencent | False | 正常单词 |
| 7 | opportunity | pass | pass | True | word | word | True |  | opportunity | True | True | 机会 |  |  | tencent | False | 正常单词 |
| 8 | significant | pass | pass | True | word | word | True |  | significant | True | True | 显著 |  |  | tencent | False | 正常单词 |
| 9 | challenge | pass | pass | True | word | word | True |  | challenge | True | True | 挑战 |  |  | tencent | False | 正常单词 |
| 10 | knowledge | pass | pass | True | word | word | True |  | knowledge | True | True | 知识 |  |  | tencent | False | 正常单词 |
| 11 | autum | warning | warning | True | word | word | True |  | autum | True | True | 秋 | 拼写疑似有误：autum。你是不是想写 “autumn”？ |  | tencent | False | 拼写弱提醒，可能提示 autumn |
| 12 | enviroment | warning | warning | True | word | word | True |  | enviroment | True | True | 环境的 | 拼写疑似有误：enviroment。你是不是想写 “environment”？ |  | tencent | False | 拼写弱提醒，可能提示 environment |
| 13 | recieve | warning | warning | True | word | word | True |  | recieve | True | True | recieve | 拼写疑似有误：recieve。你是不是想写 “receive”？ |  | tencent | False | 拼写弱提醒，可能提示 receive |
| 14 | becuase | warning | warning | True | word | word | True |  | becuase | True | True | 因为 | 拼写疑似有误：becuase。你是不是想写 “because”？ |  | tencent | False | 拼写弱提醒，可能提示 because |
| 15 | definately | warning | warning | True | word | word | True |  | definately | True | True | 绝对 | 拼写疑似有误：definately。你是不是想写 “definitely”？ |  | tencent | False | 拼写弱提醒，可能提示 definitely |
| 16 | goverment | warning | warning | True | word | word | True |  | goverment | True | True | goverment | 拼写疑似有误：goverment。你是不是想写 “government”？ |  | tencent | False | 拼写弱提醒，可能提示 government |
| 17 | seperate | warning | warning | True | word | word | True |  | seperate | True | True | 单独的 | 拼写疑似有误：seperate。你是不是想写 “separate”？ |  | tencent | False | 拼写弱提醒，可能提示 separate |
| 18 | acheive | warning | warning | True | word | word | True |  | acheive | True | True | 阿切夫 | 拼写疑似有误：acheive。你是不是想写 “achieve”？ |  | tencent | False | 拼写弱提醒，可能提示 achieve |
| 19 | frend | warning | warning | True | word | word | True |  | frend | True | True | 弗洛伊德 | 拼写疑似有误：frend。你是不是想写 “friend”？ |  | tencent | False | 拼写弱提醒，可能提示 friend |
| 20 | happyness | warning | warning | True | word | word | True |  | happyness | True | True | Happyness | 拼写疑似有误：happyness。你是不是想写 “happiness”？ |  | tencent | False | 拼写弱提醒，可能提示 happiness |
| 21 | ChatGPT | pass | pass | True | word | word | True |  | ChatGPT | True | True | ChatGPT |  |  | tencent | False | 专有名词，不应拼写误判 |
| 22 | OpenAI | pass | pass | True | word | word | True |  | OpenAI | True | True | OpenAI |  |  | tencent | False | 专有名词，不应拼写误判 |
| 23 | iPhone | pass | pass | True | word | word | True |  | iPhone | True | True | iPhone |  |  | tencent | False | 混合大小写，不应误判 |
| 24 | YouTube | pass | pass | True | word | word | True |  | YouTube | True | True | YouTube |  |  | tencent | False | 专有名词 |
| 25 | TikTok | pass | pass | True | word | word | True |  | TikTok | True | True | TikTok |  |  | tencent | False | 专有名词 |
| 26 | DeepL | pass | pass | True | word | word | True |  | DeepL | True | True | DeepL |  |  | tencent | False | 专有名词 |
| 27 | Google | pass | pass | True | word | word | True |  | Google | True | True | 谷歌 |  |  | tencent | False | 首字母大写，不应误判 |
| 28 | Microsoft | pass | pass | True | word | word | True |  | Microsoft | True | True | 微软 |  |  | tencent | False | 首字母大写，不应误判 |
| 29 | Lebron | pass | pass | True | word | word | True |  | Lebron | True | True | 勒布朗 |  |  | tencent | False | 人名，不应 error |
| 30 | London | pass | pass | True | word | word | True |  | London | True | True | 伦敦 |  |  | tencent | False | 地名，不应 error |
| 31 | API | pass | pass | True | word | word | True |  | API | True | True | API |  |  | tencent | False | 全大写缩写，不应拼写检查 |
| 32 | IELTS | pass | pass | True | word | word | True |  | IELTS | True | True | 雅思 |  |  | tencent | False | 全大写缩写 |
| 33 | TOEFL | pass | pass | True | word | word | True |  | TOEFL | True | True | 托福 |  |  | tencent | False | 全大写缩写 |
| 34 | GDP | pass | pass | True | word | word | True |  | GDP | True | True | GDP |  |  | tencent | False | 全大写缩写 |
| 35 | AI | pass | pass | True | word | word | True |  | AI | True | True | AI |  |  | tencent | False | 全大写缩写 |
| 36 | women's | pass | pass | True | word | word | True |  | women's | True | True | 妇女 |  |  | tencent | False | 所有格，不应 error |
| 37 | children's | pass | pass | True | word | word | True |  | children's | True | True | 儿童 |  |  | tencent | False | 所有格 |
| 38 | teacher's | pass | pass | True | word | word | True |  | teacher's | True | True | 老师的 |  |  | tencent | False | 所有格 |
| 39 | don't | pass | pass | True | word | word | True |  | don't | True | True | 不 |  |  | tencent | False | 缩写，不应 error |
| 40 | can't | pass | pass | True | word | word | True |  | can't | True | True | 不能 |  |  | tencent | False | 缩写，不应 error |
| 41 | GPT-4 | pass | pass | True | unknown | unknown | True |  | GPT-4 | True | True | GPT-4 |  |  | tencent | False | 含符号/数字，不应 error |
| 42 | B2B | pass | pass | True | unknown | unknown | True |  | B2B | True | True | B2b |  |  | tencent | False | 含数字术语，不应 error |
| 43 | P2P | pass | pass | True | unknown | unknown | True |  | P2P | True | True | P2p |  |  | tencent | False | 含数字术语，不应 error |
| 44 | COVID-19 | pass | pass | True | unknown | unknown | True |  | COVID-19 | True | True | COVID-19 |  |  | tencent | False | 含数字/连字符术语 |
| 45 | e-mail | pass | pass | True | unknown | unknown | True |  | e-mail | True | True | 电子邮件 |  |  | tencent | False | 连字符词，不应 error |
| 46 | look forward to | pass | pass | True | phrase | phrase | True |  | look forward to | True | True | 期待 |  |  | tencent | False | 短语 |
| 47 | give up | pass | pass | True | phrase | phrase | True |  | give up | True | True | 放弃 |  |  | tencent | False | 短语 |
| 48 | take notes | pass | pass | True | phrase | phrase | True |  | take notes | True | True | 做笔记 |  |  | tencent | False | 短语 |
| 49 | on the same page | pass | pass | True | phrase | phrase | True |  | on the same page | True | True | 在同一页上 |  |  | tencent | False | 短语 |
| 50 | break down | pass | pass | True | phrase | phrase | True |  | break down | True | True | 分解 |  |  | tencent | False | 短语 |
| 51 | pay attention to | pass | pass | True | phrase | phrase | True |  | pay attention to | True | True | 注意 |  |  | tencent | False | 短语 |
| 52 | make a difference | pass | pass | True | phrase | phrase | True |  | make a difference | True | True | 有所作为 |  |  | tencent | False | 短语 |
| 53 | in terms of | pass | pass | True | phrase | phrase | True |  | in terms of | True | True | 方面 |  |  | tencent | False | 短语 |
| 54 | as a result | pass | pass | True | phrase | phrase | True |  | as a result | True | True | 因此 |  |  | tencent | False | 短语 |
| 55 | due to | pass | pass | True | phrase | phrase | True |  | due to | True | True | 由于 |  |  | tencent | False | 短语 |
| 56 | Go away. | pass | pass | True | sentence | sentence | True |  | Go away. | True | True | 走开 |  |  | tencent | False | 短句，因句号应判 sentence |
| 57 | I love you. | pass | pass | True | sentence | sentence | True |  | I love you. | True | True | 我爱你 |  |  | tencent | False | 3 个词但有句号，应判 sentence |
| 58 | Stop it! | pass | pass | True | sentence | sentence | True |  | Stop it! | True | True | 停下来！ |  |  | tencent | False | 感叹号 sentence |
| 59 | Are you sure? | pass | pass | True | sentence | sentence | True |  | Are you sure? | True | True | 你确定吗？ |  |  | tencent | False | 问号 sentence |
| 60 | I am fine. | pass | pass | True | sentence | sentence | True |  | I am fine. | True | True | 我很好。 |  |  | tencent | False | 短句 sentence |
| 61 | This is a book. | pass | pass | True | sentence | sentence | True |  | This is a book. | True | True | 这是一本书。 |  |  | tencent | False | 普通句子 |
| 62 | I have been working on this project. | pass | pass | True | sentence | sentence | True |  | I have been working on this project. | True | True | 我一直在做这个项目。 |  |  | tencent | False | 普通句子 |
| 63 | Practice makes perfect. | pass | pass | True | sentence | sentence | True |  | Practice makes perfect. | True | True | 实践造就完美。 |  |  | tencent | False | 普通句子 |
| 64 | Time is money. | pass | pass | True | sentence | sentence | True |  | Time is money. | True | True | 时间就是金钱。 |  |  | tencent | False | 普通句子 |
| 65 | Never give up. | pass | pass | True | sentence | sentence | True |  | Never give up. | True | True | 永不放弃。 |  |  | tencent | False | 短句 sentence |
| 66 | I want to improve my English every day | pass | pass | True | sentence | sentence | True |  | I want to improve my English every day | True | True | 我想每天提高我的英语水平 |  |  | tencent | False | 6 个以上词，无标点也应 sentence |
| 67 | Reading books can help us understand the world | pass | pass | True | sentence | sentence | True |  | Reading books can help us understand the world | True | True | 读书可以帮助我们了解世界 |  |  | tencent | False | 6 个以上词 |
| 68 | This method is useful for solving difficult problems | pass | pass | True | sentence | sentence | True |  | This method is useful for solving difficult problems | True | True | 这种方法对于解决难题很有用 |  |  | tencent | False | 6 个以上词 |
| 69 | Students should learn how to manage their time | pass | pass | True | sentence | sentence | True |  | Students should learn how to manage their time | True | True | 学生应该学会如何管理自己的时间 |  |  | tencent | False | 6 个以上词 |
| 70 | The result shows that this approach is effective | pass | pass | True | sentence | sentence | True |  | The result shows that this approach is effective | True | True | 结果表明这种方法是有效的 |  |  | tencent | False | 6 个以上词 |
| 71 | hello 世界 | warning | warning | True | phrase/unknown | unknown | True |  | hello 世界 | True | True | 你好世界 | 内容包含中文，建议确认卡片英文内容是否需要拆分。 |  | tencent | False | 中英混合 warning |
| 72 | apply 申请 | warning | warning | True | phrase/unknown | unknown | True |  | apply 申请 | True | True | 申请申请 | 内容包含中文，建议确认卡片英文内容是否需要拆分。 |  | tencent | False | 中英混合 warning |
| 73 | ChatGPT 是 useful | warning | warning | True | sentence/unknown | unknown | True |  | ChatGPT 是 useful | True | True | ChatGPT很有用 | 内容包含中文，建议确认卡片英文内容是否需要拆分。 |  | tencent | False | 中英混合 warning |
| 74 | I love 你 | warning | warning | True | phrase/unknown | unknown | True |  | I love 你 | True | True | 我爱你 | 内容包含中文，建议确认卡片英文内容是否需要拆分。 |  | tencent | False | 中英混合 warning |
| 75 | good morning 早上好 | warning | warning | True | phrase/unknown | unknown | True |  | good morning 早上好 | True | True | 早上好早上好 | 内容包含中文，建议确认卡片英文内容是否需要拆分。 |  | tencent | False | 中英混合 warning |
| 76 | 你好 | error | error | True | unknown | unknown | True |  | 你好 | True | False |  |  | 内容需要包含英文，不能只有中文。 |  | False | 纯中文 error |
| 77 | 世界 | error | error | True | unknown | unknown | True |  | 世界 | True | False |  |  | 内容需要包含英文，不能只有中文。 |  | False | 纯中文 error |
| 78 | 申请 | error | error | True | unknown | unknown | True |  | 申请 | True | False |  |  | 内容需要包含英文，不能只有中文。 |  | False | 纯中文 error |
| 79 | 我爱你 | error | error | True | unknown | unknown | True |  | 我爱你 | True | False |  |  | 内容需要包含英文，不能只有中文。 |  | False | 纯中文 error |
| 80 | 这是一个句子 | error | error | True | unknown | unknown | True |  | 这是一个句子 | True | False |  |  | 内容需要包含英文，不能只有中文。 |  | False | 纯中文 error |
| 81 | 123456 | error | error | True | unknown | unknown | True |  | 123456 | True | False |  |  | 内容需要包含英文，不能只填写数字或数值。 |  | False | 纯数字 error |
| 82 | 2026 | error | error | True | unknown | unknown | True |  | 2026 | True | False |  |  | 内容需要包含英文，不能只填写数字或数值。 |  | False | 纯数字 error |
| 83 | 3.14159 | error | error | True | unknown | unknown | True |  | 3.14159 | True | False |  |  | 内容需要包含英文，不能只填写数字或数值。 |  | False | 纯数字/符号 error |
| 84 | @@@@@ | error | error | True | unknown | unknown | True |  | @@@@@ | True | False |  |  | 内容不能只有符号。 |  | False | 纯符号 error |
| 85 | !!!!!! | error | error | True | unknown | unknown | True |  | !!!!!! | True | False |  |  | 内容不能只有符号。 |  | False | 纯符号 error |
| 86 | ...... | error | error | True | unknown | unknown | True |  | ...... | True | False |  |  | 内容不能只有符号。 |  | False | 纯符号 error |
| 87 | #$%^&* | error | error | True | unknown | unknown | True |  | #$%^&* | True | False |  |  | 内容不能只有符号。 |  | False | 纯符号 error |
| 88 | abc123 | pass | pass | True | unknown | unknown | True |  | abc123 | True | True | ABC123 |  |  | tencent | False | 字母+数字，不应 error |
| 89 | hello!!! | warning | warning | True | sentence/unknown | sentence | True |  | hello!!! | True | True | 你好！ | 内容中有连续或混合标点，建议确认是否为有意输入。 |  | tencent | False | 有英文，不应 error |
| 90 | apply??? | warning | warning | True | sentence | sentence | True |  | apply??? | True | True | 申请？ | 内容中有连续或混合标点，建议确认是否为有意输入。 |  | tencent | False | 问号结尾，应 sentence |
| 91 | a | pass/warning | pass | True | word | word | True |  | a | True | True | 一 |  |  | tencent | False | 单字母，观察是否误报 |
| 92 | I | pass | pass | True | word | word | True |  | I | True | True | 我 |  |  | tencent | False | 大写单词，不应拼写误判 |
| 93 | am | pass | pass | True | word | word | True |  | am | True | True | 是 |  |  | tencent | False | 常见词 |
| 94 | the | pass | pass | True | word | word | True |  | the | True | True | 的 |  |  | tencent | False | 常见词 |
| 95 | to | pass | pass | True | word | word | True |  | to | True | True | 到 |  |  | tencent | False | 常见连接词 |
| 96 | I am on the same page with you. | pass | pass | True | sentence | sentence | True |  | I am on the same page with you. | True | True | 我和你意见一致。 |  |  | tencent | False | 句子，翻译是否自然 |
| 97 | It is important to develop a good habit of reviewing English words regularly. | pass | pass | True | sentence | sentence | True |  | It is important to develop a good habit of reviewing English words regularly. | True | True | 养成定期复习英语单词的好习惯很重要。 |  |  | tencent | False | 较长句子 |
| 98 | Although the problem looks simple, it actually requires careful analysis and repeated practice. | pass | pass | True | sentence | sentence | True |  | Although the problem looks simple, it actually requires careful analysis and repeated practice. | True | True | 问题虽然看起来简单，但实际上需要仔细分析和反复练习。 |  |  | tencent | False | 复杂句 |
| 99 | Learning English is not only about memorizing words but also about understanding how they are used in real contexts. | pass | pass | True | sentence | sentence | True |  | Learning English is not only about memorizing words but also about understanding how they are used in real contexts. | True | True | 学习英语不仅要记住单词，还要了解它们在现实环境中如何使用。 |  |  | tencent | False | 长句翻译 |
| 100 | This passage is designed to test whether the backend can correctly identify a relatively long English paragraph and give a gentle warning instead of blocking the user from saving the card, because the user may still want to keep this content for later review. | warning | warning | True | paragraph | paragraph | True |  | This passage is designed to test whether the backend can correctly identify a relatively long English paragraph and give a gentle warning instead of blocking the user from saving the card, because the user may still want to keep this content for later review. | True | True | 这段话是为了测试后台是否能正确识别一个相对较长的英文段落，并给出温和的警告，而不是阻止用户保存卡片，因为用户可能仍然希望保留这些内容以备日后查看。 | 内容较长，已按段落处理，建议复习时拆成更短的卡片。 |  | tencent | False | 长段落，应 warning，不应 error |
| 101 | I like apples, bananas, and oranges. | pass | pass | True | sentence | sentence | True |  | I like apples, bananas, and oranges. | True | True | 我喜欢苹果、香蕉和橙子。 |  |  | tencent | False | 标准逗号用法 |
| 102 | I like apples,bananas, and oranges. | pass | pass | True | sentence | sentence | True | I like apples, bananas, and oranges. | I like apples, bananas, and oranges. | True | True | 我喜欢苹果、香蕉和橙子。 |  |  | tencent | True | 逗号后缺空格 |
| 103 | I like apples,  bananas, and oranges. | pass | pass | True | sentence | sentence | True |  | I like apples, bananas, and oranges. | True | True | 我喜欢苹果、香蕉和橙子。 |  |  | tencent | True | 逗号后多个空格 |
| 104 | I like apples , bananas, and oranges. | pass | pass | True | sentence | sentence | True |  | I like apples, bananas, and oranges. | True | True | 我喜欢苹果、香蕉和橙子。 |  |  | tencent | True | 逗号前多空格 |
| 105 | I like apples ,bananas, and oranges. | pass | pass | True | sentence | sentence | True |  | I like apples, bananas, and oranges. | True | True | 我喜欢苹果、香蕉和橙子。 |  |  | tencent | True | 逗号前多空格且逗号后缺空格 |
| 106 | I like apples, bananas,and oranges. | pass | pass | True | sentence | sentence | True |  | I like apples, bananas, and oranges. | True | True | 我喜欢苹果、香蕉和橙子。 |  |  | tencent | True | 第二个逗号后缺空格 |
| 107 | I like apples ,  bananas ,  and oranges. | pass | pass | True | sentence | sentence | True |  | I like apples, bananas, and oranges. | True | True | 我喜欢苹果、香蕉和橙子。 |  |  | tencent | True | 多处逗号空格不规范 |
| 108 | This is good.But I need more practice. | pass | pass | True | sentence | sentence | True | This is good. But I need more practice. | This is good. But I need more practice. | True | True | 这很好。但我需要更多的练习。 |  |  | tencent | False | 句号后缺空格 |
| 109 | This is good. But I need more practice. | pass | pass | True | sentence | sentence | True |  | This is good. But I need more practice. | True | True | 这很好。但我需要更多的练习。 |  |  | tencent | True | 句号后正常空格 |
| 110 | This is good . But I need more practice. | pass | pass | True | sentence | sentence | True |  | This is good. But I need more practice. | True | True | 这很好。但我需要更多的练习。 |  |  | tencent | True | 句号前多空格 |
| 111 | Are you sure?Yes, I am. | pass | pass | True | sentence | sentence | True | Are you sure? Yes, I am. | Are you sure? Yes, I am. | True | True | 你确定吗？是的 |  |  | tencent | False | 问号后缺空格 |
| 112 | Are you sure? Yes, I am. | pass | pass | True | sentence | sentence | True |  | Are you sure? Yes, I am. | True | True | 你确定吗？是的 |  |  | tencent | True | 问号后正常空格 |
| 113 | Are you sure ? Yes, I am. | pass | pass | True | sentence | sentence | True |  | Are you sure? Yes, I am. | True | True | 你确定吗？是的 |  |  | tencent | True | 问号前多空格 |
| 114 | Stop it!Now! | pass | pass | True | sentence | sentence | True | Stop it! Now! | Stop it! Now! | True | True | 停下来！现在！ |  |  | tencent | False | 感叹号后缺空格 |
| 115 | Stop it! Now! | pass | pass | True | sentence | sentence | True |  | Stop it! Now! | True | True | 停下来！现在！ |  |  | tencent | True | 感叹号后正常空格 |
| 116 | Stop it ! Now! | pass | pass | True | sentence | sentence | True |  | Stop it! Now! | True | True | 停下来！现在！ |  |  | tencent | True | 感叹号前多空格 |
| 117 | I really like this book... | warning | warning | True | sentence | sentence | True |  | I really like this book... | True | True | 我真的很喜欢这本书. | 内容中有连续或混合标点，建议确认是否为有意输入。 |  | tencent | False | 连续省略点，允许但提醒 |
| 118 | I really like this book!!! | warning | warning | True | sentence | sentence | True |  | I really like this book!!! | True | True | 我真的很喜欢这本书！ | 内容中有连续或混合标点，建议确认是否为有意输入。 |  | tencent | False | 连续感叹号，提醒 |
| 119 | What are you doing??? | warning | warning | True | sentence | sentence | True |  | What are you doing??? | True | True | 你在干什么？ | 内容中有连续或混合标点，建议确认是否为有意输入。 |  | tencent | False | 连续问号，提醒 |
| 120 | This is amazing?! | warning | warning | True | sentence | sentence | True |  | This is amazing?! | True | True | 这太神奇了？！ | 内容中有连续或混合标点，建议确认是否为有意输入。 |  | tencent | False | 混合标点，提醒 |
| 121 | This is a well-known fact. | pass | pass | True | sentence | sentence | True |  | This is a well-known fact. | True | True | 这是众所周知的事实。 |  |  | tencent | False | 合法连字符复合词 |
| 122 | This is a well - known fact. | pass | pass | True | sentence | sentence | True |  | This is a well - known fact. | True | True | 这是众所周知的事实。 |  |  | tencent | False | 连字符两侧多空格 |
| 123 | This is a well- known fact. | pass | pass | True | sentence | sentence | True |  | This is a well- known fact. | True | True | 这是众所周知的事实。 |  |  | tencent | False | 连字符后多空格 |
| 124 | This is a well -known fact. | pass | pass | True | sentence | sentence | True |  | This is a well -known fact. | True | True | 这是众所周知的事实。 |  |  | tencent | False | 连字符前多空格 |
| 125 | We need a long-term plan. | pass | pass | True | sentence | sentence | True |  | We need a long-term plan. | True | True | 我们需要一个长期计划。 |  |  | tencent | False | 合法连字符短语 |
| 126 | We need a long--term plan. | pass | pass | True | sentence | sentence | True |  | We need a long--term plan. | True | True | 我们需要一个长期计划。 |  |  | tencent | False | 连续连字符异常 |
| 127 | We need a long---term plan. | pass | pass | True | sentence | sentence | True |  | We need a long---term plan. | True | True | 我们需要一个长期计划。 |  |  | tencent | False | 多重连字符异常 |
| 128 | This method is useful - but not perfect. | pass | pass | True | sentence | sentence | True |  | This method is useful - but not perfect. | True | True | 这种方法很有用，但并不完美。 |  |  | tencent | False | 普通 hyphen 当破折号，空格格式不理想 |
| 129 | This method is useful—but not perfect. | pass | pass | True | sentence | sentence | True |  | This method is useful—but not perfect. | True | True | 这种方法很有用，但并不完美。 |  |  | tencent | False | em dash 可接受 |
| 130 | This method is useful — but not perfect. | pass | pass | True | sentence | sentence | True |  | This method is useful — but not perfect. | True | True | 这种方法很有用，但并不完美。 |  |  | tencent | False | em dash 两侧空格可接受 |
| 131 | I don't know what you're talking about. | pass | pass | True | sentence | sentence | True |  | I don't know what you're talking about. | True | True | 我不知道你在说什么。 |  |  | tencent | False | 缩写和撇号 |
| 132 | I don 't know what you 're talking about. | pass | pass | True | sentence | sentence | True |  | I don't know what you're talking about. | True | True | 我不知道你在说什么。 |  |  | tencent | True | 撇号前后错误空格 |
| 133 | The student's answer is correct. | pass | pass | True | sentence | sentence | True |  | The student's answer is correct. | True | True | 学生的答案是正确的。 |  |  | tencent | False | 所有格 |
| 134 | The student 's answer is correct. | pass | pass | True | sentence | sentence | True |  | The student's answer is correct. | True | True | 学生的答案是正确的。 |  |  | tencent | True | 所有格撇号前多空格 |
| 135 | He said, "I will come back soon." | pass | pass | True | sentence | sentence | True |  | He said, "I will come back soon." | True | True | 他说：“我很快就会回来。" |  |  | tencent | False | 英文引号 |
| 136 | He said ,"I will come back soon." | pass | pass | True | sentence | sentence | True |  | He said,"I will come back soon." | True | True | 他说：“我很快就会回来。" |  |  | tencent | False | 逗号前多空格、逗号后缺空格 |
| 137 | He said, “I will come back soon.” | pass | pass | True | sentence | sentence | True |  | He said, "I will come back soon." | True | True | 他说：“我很快就会回来。" |  |  | tencent | True | 中文/弯引号可接受 |
| 138 | He said，“I will come back soon.” | pass | pass | True | sentence | sentence | True |  | He said,"I will come back soon." | True | True | 他说：“我很快就会回来。" |  |  | tencent | True | 中文逗号混入英文句 |
| 139 | I bought apples，bananas，and oranges. | pass | pass | True | sentence | sentence | True | I bought apples, bananas, and oranges. | I bought apples, bananas, and oranges. | True | True | 我买了苹果、香蕉和橙子。 |  |  | tencent | False | 中文逗号混入英文句 |
| 140 | I bought apples, bananas，and oranges. | pass | pass | True | sentence | sentence | True |  | I bought apples, bananas, and oranges. | True | True | 我买了苹果、香蕉和橙子。 |  |  | tencent | True | 中英文逗号混用 |
| 141 | This is important。Please remember it. | pass | pass | True | sentence | sentence | True |  | This is important. Please remember it. | True | True | 这很重要请记住。 |  |  | tencent | False | 中文句号混入英文句 |
| 142 | This is important. Please remember it. | pass | pass | True | sentence | sentence | True |  | This is important. Please remember it. | True | True | 这很重要请记住。 |  |  | tencent | True | 标准两句 |
| 143 | This is (very) important. | pass | pass | True | sentence | sentence | True |  | This is (very) important. | True | True | 这（非常）重要。 |  |  | tencent | False | 括号正常 |
| 144 | This is(very)important. | pass | pass | True | sentence | sentence | True |  | This is(very)important. | True | True | 这（非常）重要。 |  |  | tencent | False | 括号前后缺空格 |
| 145 | This is ( very ) important. | pass | pass | True | sentence | sentence | True |  | This is (very) important. | True | True | 这（非常）重要。 |  |  | tencent | True | 括号内部多余空格 |
| 146 | The price increased by 12.5% last year. | pass | pass | True | sentence | sentence | True |  | The price increased by 12.5% last year. | True | True | 去年价格上涨了12.5%。 |  |  | tencent | False | 英文句子中含数值百分比 |
| 147 | The meeting is on 2026-05-05. | pass | pass | True | sentence | sentence | True |  | The meeting is on 2026-05-05. | True | True | 会议于2026年5月5日举行。 |  |  | tencent | False | 英文句子中含日期 |
| 148 | I have 3.14159 reasons to doubt this result. | pass | pass | True | sentence | sentence | True |  | I have 3.14159 reasons to doubt this result. | True | True | 我有3.14159个理由怀疑这个结果。 |  |  | tencent | False | 英文句子中含小数，不应 error |
| 149 | This sentencehas a missing space. | pass | pass | True | sentence | sentence | True |  | This sentencehas a missing space. | True | True | 此广告缺少一个空间。 |  |  | tencent | False | 明显缺空格，sentencehas 可疑 |
| 150 | This sentence has     too many spaces between words. | pass | pass | True | sentence | sentence | True |  | This sentence has too many spaces between words. | True | True | 这个句子的词与词之间的空间太多了。 |  |  | tencent | False | 单词间多个空格 |
| 151 | Hello！How are you？ | pass | pass | True | sentence | sentence | True |  | Hello! How are you? | True | True | 你好！你好吗？ |  |  | tencent | False | 中文感叹号和问号 auto-fix |
| 152 | Are you sure？Yes, I am. | pass | pass | True | sentence | sentence | True |  | Are you sure? Yes, I am. | True | True | 你确定吗？是的 |  |  | tencent | True | 中文问号 auto-fix |
| 153 | Stop it！Now！ | pass | pass | True | sentence | sentence | True |  | Stop it! Now! | True | True | 停下来！现在！ |  |  | tencent | True | 中文感叹号 auto-fix |
| 154 | This is（very）important. | pass | pass | True | sentence | sentence | True |  | This is(very)important. | True | True | 这（非常）重要。 |  |  | tencent | True | 中文括号 auto-fix |
| 155 | This is “important”. | pass | pass | True | sentence | sentence | True |  | This is "important". | True | True | 这是“重要的”。 |  |  | tencent | False | 弯引号 auto-fix |
| 156 | I don‘t know. | pass | pass | True | sentence | sentence | True |  | I don't know. | True | True | 我不知道 |  |  | tencent | False | 中文撇号 auto-fix |
| 157 | The student‘s answer is correct. | pass | pass | True | sentence | sentence | True |  | The student's answer is correct. | True | True | 学生的答案是正确的。 |  |  | tencent | True | 所有格中文撇号 auto-fix |
| 158 | I don 't know. | pass | pass | True | sentence | sentence | True |  | I don't know. | True | True | 我不知道 |  |  | tencent | True | 撇号空格 auto-fix |
| 159 | The students ' books are on the desk. | pass | pass | True | sentence | sentence | True |  | The students' books are on the desk. | True | True | 学生们的书在课桌上。 |  |  | tencent | False | 复数所有格撇号空格 auto-fix |
| 160 | hello   world | pass | pass | True | phrase | phrase | True |  | hello world | True | True | Hello World |  |  | tencent | False | 多个空格 auto-fix |
| 161 | I like apples , bananas. | pass | pass | True | sentence | sentence | True |  | I like apples, bananas. | True | True | 我喜欢苹果、香蕉。 |  |  | tencent | False | 标点前空格 auto-fix |
| 162 | This is good . | pass | pass | True | sentence | sentence | True | This is good. | This is good. | True | True | 这很好。 |  |  | tencent | False | 句号前空格 auto-fix |
| 163 | state - of - the - art | pass | pass | True | phrase | phrase | True |  | state - of - the - art | True | True | 最先进的 |  |  | tencent | False | 连字符空格放过 |
| 164 | I don't know. | pass | pass | True | sentence | sentence | True |  | I don't know. | True | True | 我不知道 |  |  | tencent | True | 正常短句不应 warning |
| 165 | I see. | pass | pass | True | sentence | sentence | True |  | I see. | True | True | 我明白了. |  |  | tencent | False | 正常短句不应 warning |
| 166 | I agree. | pass | pass | True | sentence | sentence | True |  | I agree. | True | True | 我同意. |  |  | tencent | False | 正常短句不应 warning |
| 167 | Really!? | warning | warning | True | sentence | sentence | True |  | Really!? | True | True | 真的！？ | 内容中有连续或混合标点，建议确认是否为有意输入。 |  | tencent | False | 混合标点 warning |
| 168 | Wait..... | warning | warning | True | sentence | sentence | True |  | Wait..... | True | True | 等等…… | 内容中有连续或混合标点，建议确认是否为有意输入。 |  | tencent | False | 连续句点 warning |
| 169 | hello！！！ | warning | warning | True | sentence | sentence | True |  | hello!!! | True | True | 你好！ | 内容中有连续或混合标点，建议确认是否为有意输入。 |  | tencent | True | 中文感叹号 auto-fix 后连续标点 warning |
| 170 | 你好。 | error | error | True | unknown | unknown | True |  | 你好. | True | False |  |  | 内容需要包含英文，不能只有中文。 |  | False | 纯中文带中文句号 error |
| 171 | 我爱你！ | error | error | True | unknown | unknown | True |  | 我爱你! | True | False |  |  | 内容需要包含英文，不能只有中文。 |  | False | 纯中文带中文感叹号 error |
| 172 | 12.5% | error | error | True | unknown | unknown | True |  | 12.5% | True | False |  |  | 内容需要包含英文，不能只填写数字或数值。 |  | False | 纯数值百分比 error |
| 173 | 2026-05-05 | error | error | True | unknown | unknown | True |  | 2026-05-05 | True | False |  |  | 内容需要包含英文，不能只填写数字或数值。 |  | False | 纯日期数值 error |
| 174 | 10/20 | error | error | True | unknown | unknown | True |  | 10/20 | True | False |  |  | 内容需要包含英文，不能只填写数字或数值。 |  | False | 纯数值斜杠 error |
| 175 | 1:30 | error | error | True | unknown | unknown | True |  | 1:30 | True | False |  |  | 内容需要包含英文，不能只填写数字或数值。 |  | False | 纯时间数值 error |
| 176 | A4 paper | pass | pass | True | phrase | phrase | True |  | A4 paper | True | True | A4纸 |  |  | tencent | False | 英文内容中含数字不误伤 |
| 177 | I have 3 apples. | pass | pass | True | sentence | sentence | True |  | I have 3 apples. | True | True | 我有3个苹果。 |  |  | tencent | False | 英文句子中含数字不误伤 |
| 178 | The price is 12.5%. | pass | pass | True | sentence | sentence | True |  | The price is 12.5%. | True | True | 价格为12.5%。 |  |  | tencent | False | 英文句子中含百分比不误伤 |
| 179 | ？？？？ | error | error | True | unknown | unknown | True |  | ???? | True | False |  |  | 内容不能只有符号。 |  | False | 纯中文问号 auto-fix 后纯符号 error |
| 180 | ，，，， | error | error | True | unknown | unknown | True |  | , | True | False |  |  | 内容不能只有符号。 |  | False | 纯中文逗号 auto-fix 后纯符号 error |
| 181 | This is good.. | pass | pass | True | sentence | sentence | True |  | This is good. | True | True | 这很好。 |  |  | tencent | True | 两个句号 auto-fix |
| 182 | I like apples,, bananas. | pass | pass | True | sentence | sentence | True |  | I like apples, bananas. | True | True | 我喜欢苹果、香蕉。 |  |  | tencent | True | 多个逗号 auto-fix |
| 183 | ＡＢＣ１２３ | pass | pass | True | unknown | unknown | True |  | abc123 | True | True | ABC123 |  |  | tencent | True | 全角英文字母和数字 auto-fix |
| 184 | I love　English. | pass | pass | True | sentence | sentence | True |  | I love English. | True | True | 我喜欢英语。 |  |  | tencent | False | 全角空格 auto-fix |
| 185 | I love English. | pass | pass | True | sentence | sentence | True |  | I love English. | True | True | 我喜欢英语。 |  |  | tencent | True | 换行 auto-fix |
| 186 | No problem. | pass | pass | True | sentence | sentence | True |  | No problem. | True | True | 没问题. |  |  | tencent | False | 正常短句不应 warning |
| 187 | Thank you. | pass | pass | True | sentence | sentence | True |  | Thank you. | True | True | 谢谢 |  |  | tencent | False | 正常短句不应 warning |
| 188 | You're welcome. | pass | pass | True | sentence | sentence | True |  | You're welcome. | True | True | 不客气 |  |  | tencent | False | 正常短句不应 warning |
| 189 | he 'll be back. | pass | pass | True | sentence | sentence | True |  | he'll be back. | True | True | 他会回来的 |  |  | tencent | False | 缩写撇号空格 auto-fix |
| 190 | we 've done it. | pass | pass | True | sentence | sentence | True |  | we've done it. | True | True | 我们成功了 |  |  | tencent | False | 缩写撇号空格 auto-fix |
| 191 | I bought apples， bananas， and oranges. | pass | pass | True | sentence | sentence | True | I bought apples, bananas, and oranges. | I bought apples, bananas, and oranges. | True | True | 我买了苹果、香蕉和橙子。 |  |  | tencent | True | 中文逗号和逗号后空格规范化 |
| 192 | I have 3.14159 reasons. | pass | pass | True | sentence | sentence | True | I have 3.14159 reasons. | I have 3.14159 reasons. | True | True | 我有3.14159个理由。 |  |  | tencent | False | 小数不应被空格规则破坏 |
| 193 | The version is v1.2.3. | pass | pass | True | sentence | sentence | True | The version is v1.2.3. | The version is v1.2.3. | True | True | 版本为v1.2.3。 |  |  | tencent | False | 版本号不应被空格规则破坏 |
| 194 | U.S.A. | pass | pass | True | sentence | sentence | True | U.S.A. | U.S.A. | True | True | U.S.A. |  |  | tencent | False | 缩写不应被空格规则破坏 |
| 195 | Use e.g. examples carefully. | pass | pass | True | sentence | sentence | True | Use e.g. examples carefully. | Use e.g. examples carefully. | True | True | 小心使用例如例子。 |  |  | tencent | False | e.g. 缩写不应被空格规则破坏 |
