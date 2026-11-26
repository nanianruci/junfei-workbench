// Exercise the observed 1688 card shape with synthetic data and no network requests.
const {chromium} = require('../junfei-publisher/node_modules/playwright');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true});
  try {
    const context = await browser.newContext();
    await context.route('**/*', route => route.abort());
    const page = await context.newPage();
    await page.setContent('<h2>以图搜款</h2><div data-tracker="offer" data-renderkey="card_123456789012">'+
      '<div class="offer_titleText">示例门窗密封条</div><div class="offer_priceItem"><span>¥</span><span>0</span><span>.57</span></div>'+
      '<div class="offer_shopName">示例供应商</div><img width="32" height="32" src="https://cbu01.alicdn.com/demo.png"></div>');
    assert.equal(await page.locator('a[href*="/offer/"]').count(), 0);
    await page.addScriptTag({path:path.join(__dirname, 'extension/1688-search.js')});
    const result = await page.evaluate(() => collect1688SearchPage());
    assert.equal(result.offers.length, 1);
    const offer = result.offers[0];
    assert.equal(offer.offerId, '123456789012');
    assert.equal(offer.url, 'https://detail.1688.com/offer/123456789012.html');
    assert.equal(offer.priceText, '¥0.57');
    assert.equal(offer.supplierText, '示例供应商');
    assert.match(offer.title, /示例门窗密封条/);
    assert.ok(offer.imageUrl.startsWith('https://cbu01.alicdn.com/'));
    console.log('CARD_DOM_OK: card ID, title, split price ¥0.57, supplier and image extracted with no offer anchor');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
