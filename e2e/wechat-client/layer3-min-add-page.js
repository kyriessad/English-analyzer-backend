const fs = require('fs');
const path = require('path');

const artifactDir = process.env.LAYER3_MIN_ARTIFACT_DIR;
const moduleDir = process.env.LAYER3_MIN_MODULE_DIR;
const port = Number(process.env.LAYER3_MIN_PORT || '19420');
const backendLogPath = process.env.LAYER3_MIN_BACKEND_LOG;

if (!artifactDir || !moduleDir || !backendLogPath) {
  throw new Error('Missing LAYER3_MIN_* environment variables');
}

fs.mkdirSync(artifactDir, { recursive: true });

function now() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function writeJson(name, value) {
  fs.writeFileSync(
    path.join(artifactDir, name),
    `${JSON.stringify(value, null, 2)}\n`,
    'utf8',
  );
}

function readBackendLogFrom(offset) {
  if (!fs.existsSync(backendLogPath)) {
    return { offset, text: '' };
  }
  const buffer = fs.readFileSync(backendLogPath);
  return {
    offset: buffer.length,
    text: buffer.slice(offset).toString('utf8'),
  };
}

async function withTimeout(promise, timeoutMs, label) {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs}ms`)), timeoutMs);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function pageData(page) {
  if (page && typeof page.data === 'function') {
    return await page.data();
  }
  return page && page.data ? page.data : {};
}

async function relaunchAdd(miniProgram) {
  if (typeof miniProgram.reLaunch === 'function') {
    await miniProgram.reLaunch('/pages/add/add');
  } else if (typeof miniProgram.callWxMethod === 'function') {
    await miniProgram.callWxMethod('reLaunch', { url: '/pages/add/add' });
  } else {
    throw new Error('No reLaunch capability available');
  }
  await sleep(1500);
  return await withTimeout(miniProgram.currentPage(), 15000, 'currentPage after reLaunch');
}

async function setCategory(page, category) {
  const categories = ['单词', '短语', '句子'];
  const index = Math.max(categories.indexOf(category), 0);
  if (typeof page.callMethod === 'function') {
    await page.callMethod('onCategoryChange', { detail: { value: index } });
  } else if (typeof page.setData === 'function') {
    await page.setData({ categoryIndex: index, 'form.category': categories[index] });
  }
  await sleep(300);
}

async function inputEnglish(page, text) {
  const textarea = await page.$('.textarea-english');
  if (textarea && typeof textarea.input === 'function') {
    try {
      await textarea.input(text);
    } catch (error) {
      if (typeof page.callMethod !== 'function') throw error;
      await page.callMethod('onEnglishInput', { detail: { value: text } });
    }
  } else if (typeof page.callMethod === 'function') {
    await page.callMethod('onEnglishInput', { detail: { value: text } });
  } else {
    throw new Error('No input capability available');
  }
}

async function waitForValidation(page, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const data = await pageData(page);
    if (['pass', 'warning', 'invalid', 'unavailable'].includes(data.validationStatus)) {
      return data;
    }
    await sleep(500);
  }
  return await pageData(page);
}

async function waitForAnalysis(page, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const data = await pageData(page);
    if (!data.translating && !data.isAnalyzing && (data.suggestionText || data.aiServiceMessage)) {
      return data;
    }
    await sleep(1000);
  }
  return await pageData(page);
}

function summarizeData(data) {
  return {
    validationStatus: data.validationStatus,
    validationCanSave: data.validationCanSave,
    validationCanAnalyze: data.validationCanAnalyze,
    validationCanPronounce: data.validationCanPronounce,
    validationIssues: data.validationIssues || [],
    validationVisibleIssues: data.validationVisibleIssues || [],
    validationNormalizedText: data.validationNormalizedText,
    suggestionText: data.suggestionText || '',
    aiServiceMessage: data.aiServiceMessage || '',
    aiExampleSentence: data.aiExampleSentence || '',
    aiExampleTranslation: data.aiExampleTranslation || '',
    aiDialogueEnglish: data.aiDialogueEnglish || [],
    aiRelatedDisplay: data.aiRelatedDisplay || '',
    aiAlternativeMeanings: data.aiAlternativeMeanings || [],
    aiUsageScenario: data.aiUsageScenario || '',
    aiAnalysisCategory: data.aiAnalysisCategory || '',
    aiParagraphAnalysis: data.aiParagraphAnalysis || '',
    isAnalyzing: data.isAnalyzing,
    isRegenerating: data.isRegenerating,
    translating: data.translating,
  };
}

function analyzeLog(text) {
  return {
    validateRequests: (text.match(/\/api\/validate-english/g) || []).length,
    analyzeRequests: (text.match(/\/api\/analyze-english/g) || []).length,
    ttsRequests: (text.match(/\/api\/tts|\/api\/pronunciation|\/api\/synthesize/g) || []).length,
    cacheBypass: (text.match(/result='?bypass'?|result=bypass|"result":"bypass"/g) || []).length,
    cacheHit: (text.match(/result='?hit'?|result=hit|"result":"hit"/g) || []).length,
    cacheMiss: (text.match(/result='?miss'?|result=miss|"result":"miss"/g) || []).length,
    generationAttempts: (text.match(/generation_attempt/g) || []).length,
    ollamaStarts: (text.match(/ollama_generation_start/g) || []).length,
    contentWarning: (text.match(/CONTENT_WARNING/g) || []).length,
    advisoryWarning: (text.match(/ADVISORY_WARNING/g) || []).length,
    forceRefresh: (text.match(/forceRefresh":true|force_refresh=true|force_refresh.:.?true/g) || []).length,
  };
}

async function validateCase(miniProgram, name, sample, category) {
  const page = await relaunchAdd(miniProgram);
  await setCategory(page, category);
  const start = fs.existsSync(backendLogPath) ? fs.statSync(backendLogPath).size : 0;
  await inputEnglish(page, sample);
  const data = await waitForValidation(page);
  const log = readBackendLogFrom(start);
  return {
    name,
    sample,
    category,
    ui: summarizeData(data),
    backend: analyzeLog(log.text),
    backendLogTail: log.text.slice(-3000),
  };
}

async function analyzeCase(miniProgram, name, sample, category, regenerate) {
  const page = await relaunchAdd(miniProgram);
  await setCategory(page, category);
  const start = fs.existsSync(backendLogPath) ? fs.statSync(backendLogPath).size : 0;
  await inputEnglish(page, sample);
  await waitForValidation(page);
  const analyzeButton = await page.$('.ai-analyze-btn');
  if (!analyzeButton || typeof analyzeButton.tap !== 'function') {
    throw new Error('AI analyze button not available');
  }
  await analyzeButton.tap();
  const first = await waitForAnalysis(page);
  let second = null;
  if (regenerate) {
    const regen = await page.$('.reference-regenerate-btn');
    if (!regen || typeof regen.tap !== 'function') {
      throw new Error('Regenerate button not available');
    }
    await regen.tap();
    second = await waitForAnalysis(page);
  }
  const log = readBackendLogFrom(start);
  return {
    name,
    sample,
    category,
    firstUi: summarizeData(first),
    secondUi: second ? summarizeData(second) : null,
    backend: analyzeLog(log.text),
    backendLogTail: log.text.slice(-5000),
  };
}

async function main() {
  const automator = require(path.join(moduleDir, 'node_modules', 'miniprogram-automator'));
  const miniProgram = await withTimeout(
    automator.connect({ wsEndpoint: `ws://127.0.0.1:${port}` }),
    15000,
    'connect',
  );

  const result = {
    startedAt: now(),
    port,
    cases: [],
    console: [],
  };
  writeJson('layer3-min-add-page-result.json', result);

  if (typeof miniProgram.on === 'function') {
    miniProgram.on('console', (message) => {
      result.console.push(String(message && message.args ? message.args.join(' ') : message));
    });
  }

  try {
    const cases = [
      () => validateCase(miniProgram, 'control_character', 'hello\u0000world', '句子'),
      () => validateCase(miniProgram, 'harper_grammar_usage', 'I goed there yesterday.', '句子'),
      () => validateCase(miniProgram, 'punctuation_anomaly', 'Really?!', '句子'),
      () => validateCase(miniProgram, 'category_mismatch', 'give up', '单词'),
      () => analyzeCase(miniProgram, 'sentence_short_full_analysis', 'I love English.', '句子', false),
      () => analyzeCase(
        miniProgram,
        'sentence_long_translation_only',
        'This is a longer sentence that should only be translated because it has enough characters to exceed the short sentence analysis boundary for learners.',
        '句子',
        false,
      ),
      () => analyzeCase(miniProgram, 'regenerate_bypass_cache', 'give up', '短语', true),
    ];

    for (const runCase of cases) {
      try {
        const item = await runCase();
        result.cases.push(item);
        writeJson('layer3-min-add-page-result.json', result);
      } catch (error) {
        result.cases.push({
          name: 'case_failed',
          failedAt: now(),
          fatal: {
            type: error && error.name ? error.name : 'Error',
            message: String(error && error.message ? error.message : error),
            stack: String(error && error.stack ? error.stack : error),
          },
        });
        writeJson('layer3-min-add-page-result.json', result);
        throw error;
      }
    }
  } finally {
    if (miniProgram && typeof miniProgram.disconnect === 'function') {
      miniProgram.disconnect();
    }
  }

  result.finishedAt = now();
  writeJson('layer3-min-add-page-result.json', result);
  console.log(JSON.stringify({
    resultPath: path.join(artifactDir, 'layer3-min-add-page-result.json'),
    caseCount: result.cases.length,
  }, null, 2));
}

main().catch((error) => {
  let existing = {};
  try {
    existing = JSON.parse(fs.readFileSync(path.join(artifactDir, 'layer3-min-add-page-result.json'), 'utf8'));
  } catch (_) {}
  const failure = {
    ...existing,
    finishedAt: now(),
    fatal: {
      type: error && error.name ? error.name : 'Error',
      message: String(error && error.message ? error.message : error),
      stack: String(error && error.stack ? error.stack : error),
    },
  };
  writeJson('layer3-min-add-page-result.json', failure);
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
