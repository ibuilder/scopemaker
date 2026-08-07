/* ScopeMaker static demo.
 *
 * GitHub Pages cannot run Flask, so this assembles an exhibit in the browser.
 * The *data* is library.json, exported from the real seed library and
 * regenerated in CI -- so the clauses, the cross-division specification
 * references and the defaults are exactly what the application uses. Only the
 * assembly and numbering are reimplemented here, and they are kept
 * deliberately small so they are easy to check against the Python.
 *
 * What this demo does NOT do, and the real application does: persist anything,
 * authenticate anyone, render a PDF or DOCX, or run the project-wide coverage
 * analysis. Those need a server.
 */
(function () {
  'use strict';

  var LIB = null;
  var state = { division: '21', clauses: new Set(), specs: new Set() };

  var SECTION_ORDER = [
    { key: 'intent',         heading: 'Intent',                            kind: 'prose' },
    { key: 'summary',        heading: 'Scope of Work Summary',             kind: 'items' },
    { key: 'inclusions',     heading: 'Trade Specific Scope of Work Items', kind: 'items' },
    { key: 'exclusions',     heading: 'Trade Specific Scope Exclusions',    kind: 'items' },
    { key: 'clarifications', heading: 'Clarifications and Assumptions',     kind: 'items' },
    { key: 'closeout',       heading: 'Closeout Requirements',              kind: 'items' },
    { key: 'safety',         heading: 'Safety Requirements',                kind: 'items' },
    { key: 'schedule',       heading: 'Schedule Requirements',              kind: 'items' },
    { key: 'recap',          heading: 'Recap of Contract Amount',           kind: 'recap' }
  ];

  // Mirrors CATEGORY_TO_SECTION in scopemaker/models/scope.py.
  var CATEGORY_SECTION = {
    inclusion: 'inclusions',
    exclusion: 'exclusions',
    clarification: 'clarifications',
    general_requirement: 'summary',
    safety: 'safety',
    closeout: 'closeout',
    schedule: 'schedule',
    allowance: 'inclusions',
    alternate: 'inclusions',
    unit_price: 'inclusions'
  };

  /* ---------------------------------------------------------------- helpers */
  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function divisionOf(code) {
    return LIB.divisions.filter(function (d) { return d.code === code; })[0] || null;
  }

  function tradeFor(code) {
    var d = divisionOf(code);
    return (d && d.trades && d.trades[0]) || (d && d.title) || '';
  }

  /* Merge {placeholder} tokens, exactly as scope_builder.render_template_text
   * does -- including rendering an unavailable value as a visible blank rather
   * than a sentence that reads as complete. */
  function merge(text, context) {
    return String(text || '').replace(/\{([a-z0-9_]+)\}/g, function (whole, key) {
      if (!(key in context)) { return whole; }
      return context[key] || '__________';
    });
  }

  /* Which clauses apply: this division's, plus the universal ones. */
  function availableClauses(code) {
    return LIB.clauses
      .filter(function (c) { return c.division === null || c.division === code; })
      .sort(function (a, b) {
        var order = Object.keys(LIB.categories);
        var byCat = order.indexOf(a.category) - order.indexOf(b.category);
        if (byCat) { return byCat; }
        // Universal obligations before trade detail, mirroring the server.
        var byScope = (a.division === null ? 0 : 1) - (b.division === null ? 0 : 1);
        if (byScope) { return byScope; }
        return a.position - b.position;
      });
  }

  /* Sections offered to this division: its own, the universal Division 01
   * ones, and anything cross-referenced to it. That last group is the point. */
  function availableSpecs(code) {
    return LIB.spec_sections
      .filter(function (s) {
        return s.universal || s.division === code || s.related.indexOf(code) !== -1;
      })
      .sort(function (a, b) {
        var group = function (s) { return s.division === code ? 0 : (s.universal ? 1 : 2); };
        var byGroup = group(a) - group(b);
        if (byGroup) { return byGroup; }
        if (a.division !== b.division) { return a.division < b.division ? -1 : 1; }
        return a.position - b.position;
      });
  }

  function selectDefaults(code) {
    state.clauses = new Set(
      availableClauses(code).filter(function (c) { return c.default; })
        .map(function (c) { return c.key; })
    );
    state.specs = new Set(
      availableSpecs(code).filter(function (s) { return s.default; })
        .map(function (s) { return s.key; })
    );
  }

  /* ------------------------------------------------------------- numbering */
  /* Legal scheme: 1., 1.1, 1.1.1 -- see scopemaker/services/numbering.py. */
  function label(counters) {
    var text = counters.join('.');
    return counters.length === 1 ? text + '.' : text;
  }

  /* -------------------------------------------------------------- assembly */
  function buildDocument() {
    var code = state.division;
    var trade = tradeFor(code);
    var context = {
      trade: trade,
      trade_upper: trade.toUpperCase(),
      division_code: code,
      division_title: (divisionOf(code) || {}).title || '',
      currency: 'USD',
      project_name: 'Riverside Medical Center',
      project_number: '2024-118',
      project_location: '1400 River Road, Columbus, OH',
      owner_name: 'Riverside Health System',
      architect_name: 'Whitfield Architects',
      contractor_name: 'Meridian Construction Group, LLC',
      bid_package_number: 'BP-' + code + 'A',
      bid_package_name: trade,
      subcontractor_name: ''
    };

    var chosenClauses = availableClauses(code)
      .filter(function (c) { return state.clauses.has(c.key); });
    var chosenSpecs = availableSpecs(code)
      .filter(function (s) { return state.specs.has(s.key); });

    var bySection = {};
    chosenClauses.forEach(function (c) {
      var key = CATEGORY_SECTION[c.category] || 'inclusions';
      (bySection[key] = bySection[key] || []).push(c);
    });

    var sections = [];
    SECTION_ORDER.forEach(function (def) {
      var items = [];

      if (def.key === 'summary') {
        items.push({ text: merge(LIB.boilerplate.summary_lead, context) });
        items.push({ text: merge(LIB.boilerplate.summary_means_and_methods, context) });
        if (chosenSpecs.length) {
          items.push({
            text: merge(LIB.boilerplate.summary_spec_lead, context),
            children: chosenSpecs.map(function (s) {
              return { text: s.code + ' &ndash; ' + esc(s.title), foreign: s.division !== code };
            })
          });
        }
        (bySection.summary || []).forEach(function (c) { items.push({ text: esc(c.text) }); });
      } else if (def.kind === 'items') {
        (bySection[def.key] || []).forEach(function (c) { items.push({ text: esc(c.text) }); });
      }

      var lead = def.key === 'intent'
        ? LIB.boilerplate.intent
        : LIB.boilerplate[def.key + '_lead'];

      if (def.kind === 'recap' || items.length || (def.key === 'intent' && lead)) {
        sections.push({
          key: def.key,
          heading: def.heading,
          kind: def.kind,
          body: def.key === 'summary' ? '' : merge(lead || '', context),
          items: items
        });
      }
    });

    return { sections: sections, context: context, itemCount: countItems(sections) };
  }

  function countItems(sections) {
    var total = 0;
    sections.forEach(function (s) {
      s.items.forEach(function (i) {
        total += 1 + ((i.children || []).length);
      });
    });
    return total;
  }

  /* --------------------------------------------------------------- render */
  function renderItems(items, prefix) {
    var html = '<ol class="doc__list">';
    items.forEach(function (item, index) {
      var counters = prefix.concat([index + 1]);
      html += '<li class="doc__item doc__item--depth-' + (counters.length - 1) + '">' +
        '<span class="doc__item-label">' + label(counters) + '</span>' +
        '<span class="doc__item-text">' + item.text +
        (item.foreign ? ' <em class="foreign">&larr; Division ' + item.text.slice(0, 2) + '</em>' : '') +
        '</span></li>';
      if (item.children && item.children.length) {
        html += '<li class="doc__item">' + renderItems(item.children, counters) + '</li>';
      }
    });
    return html + '</ol>';
  }

  function renderDocument(doc) {
    var c = doc.context;
    var html = '<article class="doc doc--draft">';
    html += '<h1 class="doc__title">EXHIBIT B – Scope of Work</h1>';
    html += '<p class="doc__subtitle">Division ' + c.division_code + ' – ' + esc(c.trade) + '</p>';
    html += '<table class="doc__facts"><tbody>' +
      '<tr><th scope="row">Project</th><td>' + c.project_number + '  ' + c.project_name + '</td></tr>' +
      '<tr><th scope="row">Location</th><td>' + c.project_location + '</td></tr>' +
      '<tr><th scope="row">Owner</th><td>' + c.owner_name + '</td></tr>' +
      '<tr><th scope="row">Bid Package</th><td>' + c.bid_package_number + '  ' + esc(c.bid_package_name) + '</td></tr>' +
      '</tbody></table>';

    doc.sections.forEach(function (section, index) {
      html += '<section class="doc__section">';
      html += '<h2 class="doc__heading"><span class="doc__heading-number">' +
        (index + 1) + '.</span>' + section.heading + '</h2>';
      if (section.body) { html += '<div class="doc__body">' + section.body + '</div>'; }

      if (section.kind === 'recap') {
        html += '<table class="doc__recap"><tbody>' +
          '<tr><td>' + c.bid_package_number + ' – Base Bid Amount</td><td>$ TBD</td></tr>' +
          '<tr><td>Add: Accepted Alternates</td><td>$ TBD</td></tr>' +
          '<tr><td>Other Additions / Deletions</td><td>$ TBD</td></tr>' +
          '<tr><td>TOTAL SUBCONTRACT AMOUNT</td><td>$ TBD</td></tr>' +
          '</tbody></table>';
      } else if (section.items.length) {
        html += renderItems(section.items, []);
      }
      html += '</section>';
    });

    return html + '</article>';
  }

  /* ---------------------------------------------------------------- picker */
  function renderPicker() {
    var code = state.division;
    var specs = availableSpecs(code);
    var clauses = availableClauses(code);

    var html = '';

    var crossRefs = specs.filter(function (s) {
      return !s.universal && s.division !== code;
    });

    html += '<div class="picker-group"><div class="picker-head">' +
      '<strong>Specification sections</strong>' +
      '<span class="muted">' + state.specs.size + ' of ' + specs.length + '</span>' +
      '</div><div class="picker-list">';
    specs.forEach(function (s) {
      var foreign = !s.universal && s.division !== code;
      html += '<label class="picker-item"><input type="checkbox" data-spec="' + s.key + '"' +
        (state.specs.has(s.key) ? ' checked' : '') + '>' +
        '<span><code>' + s.code + '</code> ' + esc(s.title) +
        (foreign ? ' <span class="tag tag--cross">Division ' + s.division + '</span>' : '') +
        (s.universal ? ' <span class="tag">Div 01</span>' : '') +
        '</span></label>';
    });
    html += '</div></div>';

    var byCategory = {};
    clauses.forEach(function (cl) {
      (byCategory[cl.category] = byCategory[cl.category] || []).push(cl);
    });

    Object.keys(LIB.categories).forEach(function (category) {
      var group = byCategory[category];
      if (!group || !group.length) { return; }
      var selected = group.filter(function (c) { return state.clauses.has(c.key); }).length;
      html += '<div class="picker-group"><div class="picker-head">' +
        '<strong>' + esc(LIB.categories[category]) + '</strong>' +
        '<span class="muted">' + selected + ' of ' + group.length + '</span>' +
        '</div><div class="picker-list">';
      group.forEach(function (cl) {
        html += '<label class="picker-item"><input type="checkbox" data-clause="' + cl.key + '"' +
          (state.clauses.has(cl.key) ? ' checked' : '') + '>' +
          '<span>' + esc(cl.text) +
          (cl.division === null ? ' <span class="tag">All trades</span>' : '') +
          '</span></label>';
      });
      html += '</div></div>';
    });

    document.getElementById('picker').innerHTML = html;

    document.getElementById('crossref-count').textContent = crossRefs.length;
    document.getElementById('crossref-list').innerHTML = crossRefs.length
      ? crossRefs.map(function (s) {
          return '<li><code>' + s.code + '</code> ' + esc(s.title) +
            ' <span class="tag tag--cross">Division ' + s.division + '</span></li>';
        }).join('')
      : '<li class="muted">None for this division.</li>';
  }

  function refresh() {
    var doc = buildDocument();
    document.getElementById('preview').innerHTML = renderDocument(doc);
    document.getElementById('item-count').textContent = doc.itemCount;
    document.getElementById('clause-count').textContent = state.clauses.size;
    document.getElementById('spec-count').textContent = state.specs.size;
  }

  function rebuild() {
    renderPicker();
    refresh();
  }

  /* ------------------------------------------------------------------ init */
  function init(library) {
    LIB = library;

    var select = document.getElementById('division');
    LIB.divisions.forEach(function (d) {
      var option = document.createElement('option');
      option.value = d.code;
      option.textContent = d.code + ' — ' + d.title;
      if (d.code === state.division) { option.selected = true; }
      select.appendChild(option);
    });

    select.addEventListener('change', function () {
      state.division = select.value;
      selectDefaults(state.division);
      rebuild();
    });

    document.getElementById('picker').addEventListener('change', function (event) {
      var input = event.target;
      var clause = input.getAttribute('data-clause');
      var spec = input.getAttribute('data-spec');
      if (clause) {
        if (input.checked) { state.clauses.add(clause); } else { state.clauses.delete(clause); }
      } else if (spec) {
        if (input.checked) { state.specs.add(spec); } else { state.specs.delete(spec); }
      }
      renderPicker();
      refresh();
    });

    document.getElementById('reset').addEventListener('click', function () {
      selectDefaults(state.division);
      rebuild();
    });

    document.getElementById('stats').textContent =
      LIB.clauses.length + ' clauses · ' + LIB.spec_sections.length +
      ' specification sections · ' + LIB.divisions.length + ' divisions';

    selectDefaults(state.division);
    rebuild();
    document.getElementById('loading').remove();
    document.getElementById('app').hidden = false;
  }

  fetch('library.json')
    .then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
    })
    .then(init)
    .catch(function (err) {
      document.getElementById('loading').innerHTML =
        '<p class="muted">Could not load the clause library (' + esc(err.message) +
        '). The demo needs to be served over HTTP — opening the file directly ' +
        'from disk will not work.</p>';
    });
})();
