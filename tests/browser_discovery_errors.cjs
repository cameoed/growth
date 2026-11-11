const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/beings_leads/dashboard/app.js', 'utf8');
const code = source.slice(source.indexOf('function discoveryErrorSummary('), source.indexOf('function discoveryRunsMarkup('));
const context = vm.createContext({URL, Set, Number, String, Math,
  ACTIVE_RUN_STATES: new Set(['running']),
  firstValue: (obj, paths, fallback) => {
    for (const path of paths) {
      const value = path.split('.').reduce((o, key) => o?.[key], obj);
      if (value != null) return value;
    }
    return fallback;
  },
  formatNumber: String, humanize: String,
  escapeHtml: value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  relativeTime: () => 'now',
});
vm.runInContext(code, context);
const summarize = context.discoveryErrorSummary;
const repeated = [1,2,3,4,5].map(n => `Customer list: https://www.contentful.com/case-studies/${n}: ProviderRateLimitDeferred: www.contentful.com HTTP 429: rate limited; request deferred locally; retry after 890 seconds`).join('; ');
const result = summarize(repeated);
assert.equal((result.match(/contentful.com/g) || []).length, 1);
assert.ok(result.length < 150);
assert.match(summarize(repeated + '; https://other.example: TimeoutError: timed out'), /TimeoutError/);
assert.equal(summarize('Unexpected parse error'), 'Unexpected parse error');
assert.equal(summarize(null), '');
const markup = context.discoveryRunMarkup({method:'customer_lists',status:'failed',pages_fetched:1,documents_fetched:2,error:repeated});
assert.match(markup, /1<\/strong> page<\/span>/);
assert.match(markup, /2<\/strong> documents<\/span>/);
assert.ok(!source.includes('Paid scan history'));
assert.ok(!source.includes('renderAdDiscoveryHistory'));
console.log('Discovery error presentation checks passed');
