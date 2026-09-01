const path = require('path');

const moduleDir = __dirname;
const port = Number(process.env.WECHAT_AUTOMATION_PORT || '19430');
const backendBase = String(process.env.FORMAL_BACKEND_BASE || '').replace(/\/$/, '');
const automator = require(path.join(moduleDir, 'node_modules', 'miniprogram-automator'));

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(label, read, accept, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let value;
  while (Date.now() < deadline) {
    value = await read();
    if (accept(value)) return value;
    await sleep(350);
  }
  throw new Error(`${label} timed out; last=${JSON.stringify(value)}`);
}

async function pageData(page) {
  return typeof page.data === 'function' ? page.data() : page.data;
}

async function apiGet(token, pathname) {
  const started = Date.now();
  const response = await fetch(`${backendBase}${pathname}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      'ngrok-skip-browser-warning': '1',
    },
  });
  const body = await response.json();
  return {
    path: pathname,
    status: response.status,
    elapsed_ms: Date.now() - started,
    item_count: Array.isArray(body.items) ? body.items.length : undefined,
    total: body.total,
    quote_content: body.item && body.item.content,
  };
}

async function main() {
  if (!backendBase) throw new Error('FORMAL_BACKEND_BASE is required');
  const miniProgram = await automator.connect({ wsEndpoint: `ws://127.0.0.1:${port}` });
  const consoleMessages = [];
  let discoveryNavigationSucceeded = true;
  miniProgram.on('console', (message) => {
    consoleMessages.push(String(message && message.args ? message.args.join(' ') : message));
  });

  try {
    await miniProgram.reLaunch('/pages/index/index');
    let home = await miniProgram.currentPage();
    const homeData = await waitFor(
      'home quote',
      () => pageData(home),
      (data) => Boolean(data.todayQuote) && !data.todayQuoteLoading,
      30000,
    );

    const token = await miniProgram.callWxMethod('getStorageSync', 'backendAccessToken');
    if (!token || typeof token !== 'string') throw new Error('real WeChat login did not produce a stored token');

    const api = [];
    api.push(await apiGet(token, '/api/discovery/packs'));
    api.push(await apiGet(token, '/api/discovery/items?pack=cet4&limit=20&offset=0'));
    api.push(await apiGet(token, '/api/discovery/today-quote'));
    const cardsBefore = await apiGet(token, '/api/cards?limit=100&offset=0');

    const discoverEntry = await home.$('#discover-entry');
    if (!discoverEntry) throw new Error('discover entry missing');
    await discoverEntry.tap();
    let discover = await waitFor(
      'discover page',
      () => miniProgram.currentPage(),
      (page) => page && page.path === 'pages/discover/index',
    );
    let discoverData = await waitFor(
      'discovery content',
      () => pageData(discover),
      (data) => !data.loading && data.packs.length === 11 && data.items.length > 0,
      30000,
    );

    const dailyLifePack = await discover.$('#discover-pack-daily-life');
    if (!dailyLifePack) throw new Error('daily-life pack control missing');
    await dailyLifePack.tap();
    discoverData = await waitFor(
      'pack filter',
      () => pageData(discover),
      (data) => data.selectedPackCode === 'daily-life' && !data.loading && data.items.length > 0,
    );
    const searchInput = await discover.$('input');
    if (!searchInput) throw new Error('discovery search input missing');
    await searchInput.input('window');
    await sleep(800);
    const searchedData = await waitFor(
      'search',
      () => pageData(discover),
      (data) => !data.loading && data.searchText === 'window',
    );
    const searched = { searchText: searchedData.searchText, total: searchedData.total };
    await searchInput.input('');
    await sleep(1000);
    discoverData = await waitFor(
      'search clear',
      () => pageData(discover),
      (data) => !data.loading && data.searchText === '' && data.items.length > 1,
    );

    const knownItem = discoverData.items[0];
    const knownButtons = await discover.$$('.known-btn');
    if (!knownButtons[0]) throw new Error('known button missing');
    await knownButtons[0].tap();
    const afterKnown = await waitFor(
      'known state',
      () => pageData(discover),
      (data) => !data.markingItemId && !data.items.some((item) => item.id === knownItem.id),
    );

    const rememberItem = afterKnown.items.find((item) => !item.in_library);
    if (!rememberItem) throw new Error('no non-library discovery item available for remember flow');
    const rememberButtons = await discover.$$('.remember-btn');
    const rememberIndex = afterKnown.items.findIndex((item) => item.id === rememberItem.id);
    const rememberButton = rememberButtons[rememberIndex];
    if (!rememberButton) throw new Error('remember button missing for selected item');
    await rememberButton.tap();
    await sleep(12000);
    const storedDiscoveryPrefill = await miniProgram.callWxMethod(
      'getStorageSync',
      'englishCard.discoveryPrefill.v1',
    );
    const pageAfterRemember = await miniProgram.currentPage();
    if (!pageAfterRemember || pageAfterRemember.path !== 'pages/add/add') {
      discoveryNavigationSucceeded = false;
      const pageStack = await miniProgram.evaluate(function () {
        return getCurrentPages().map(function (page) { return page.route; });
      });
      const navigationProbe = await miniProgram.evaluate(function () {
        return new Promise(function (resolve) {
          wx.navigateTo({
            url: '/pages/add/add?from=discovery',
            success: function () { resolve({ ok: true }); },
            fail: function (error) { resolve({ ok: false, error: error && error.errMsg }); },
          });
        });
      });
      const reLaunchProbe = !navigationProbe || !navigationProbe.ok
        ? await miniProgram.evaluate(function () {
            return new Promise(function (resolve) {
              wx.reLaunch({
                url: '/pages/add/add?from=discovery',
                success: function () { resolve({ ok: true }); },
                fail: function (error) { resolve({ ok: false, error: error && error.errMsg }); },
              });
            });
          })
        : null;
      if ((!navigationProbe || !navigationProbe.ok) && (!reLaunchProbe || !reLaunchProbe.ok)) throw new Error(`remember did not navigate: ${JSON.stringify({
        item: rememberItem,
        storedDiscoveryPrefill,
        page: pageAfterRemember && pageAfterRemember.path,
        pageStack,
        navigationProbe,
        reLaunchProbe,
        consoleTail: consoleMessages.slice(-20),
      })}`);
    }
    const addFromDiscovery = await waitFor(
      'discovery prefill navigation',
      () => miniProgram.currentPage(),
      (page) => page && page.path === 'pages/add/add',
    );
    await sleep(800);
    const discoveryPrefill = await pageData(addFromDiscovery);

    await miniProgram.reLaunch('/pages/index/index');
    home = await miniProgram.currentPage();
    const refreshedHome = await waitFor(
      'home quote after discovery',
      () => pageData(home),
      (data) => Boolean(data.todayQuote) && !data.todayQuoteLoading,
    );
    const quoteButton = await home.$('#today-quote-remember');
    if (!quoteButton) throw new Error('today quote remember button missing');
    await quoteButton.tap();
    const addFromQuote = await waitFor(
      'quote prefill navigation',
      () => miniProgram.currentPage(),
      (page) => page && page.path === 'pages/add/add',
    );
    await sleep(800);
    const quotePrefill = await pageData(addFromQuote);
    const cardsAfter = await apiGet(token, '/api/cards?limit=100&offset=0');

    const result = {
      home: {
        quote_visible: Boolean(homeData.todayQuote && homeData.todayQuote.content),
        quote_content: homeData.todayQuote && homeData.todayQuote.content,
      },
      api,
      discovery: {
        packs: discoverData.packs.length,
        selected_pack: discoverData.selectedPackCode,
        visible_items: discoverData.items.length,
        search_keyword: searched.searchText,
        search_total: searched.total,
        known_item_removed: !afterKnown.items.some((item) => item.id === knownItem.id),
      },
      discovery_prefill: {
        english: discoveryPrefill.form && discoveryPrefill.form.englishText,
        chinese: discoveryPrefill.form && discoveryPrefill.form.myUnderstanding,
        source: discoveryPrefill.form && discoveryPrefill.form.whereEncountered,
        navigation_succeeded: discoveryNavigationSucceeded,
      },
      quote_prefill: {
        english: quotePrefill.form && quotePrefill.form.englishText,
        chinese: quotePrefill.form && quotePrefill.form.myUnderstanding,
        source: quotePrefill.form && quotePrefill.form.whereEncountered,
        matches_quote: Boolean(
          quotePrefill.form && quotePrefill.form.englishText === refreshedHome.todayQuote.content
        ),
      },
      public_material_did_not_create_card: cardsBefore.total === cardsAfter.total,
      card_total_before: cardsBefore.total,
      card_total_after: cardsAfter.total,
      timeout_console_messages: consoleMessages.filter((line) => /timeout|timed out/i.test(line)),
    };
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } finally {
    miniProgram.disconnect();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
