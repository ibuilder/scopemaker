/* Poll an async render until it is ready, then start the download.
 *
 * Backs off as it waits so a queue that is genuinely busy is not hammered, and
 * gives up after a couple of minutes rather than spinning forever -- at which
 * point the user is told plainly instead of being left watching a spinner.
 */
(function () {
  'use strict';

  var script = document.currentScript ||
    document.querySelector('script[data-state-url]');
  if (!script) { return; }

  var stateUrl = script.getAttribute('data-state-url');
  var downloadUrl = script.getAttribute('data-download-url');
  var delay = 700;
  var elapsed = 0;
  var GIVE_UP_AFTER = 120000;

  function say(text, isError) {
    var el = document.getElementById('status-text');
    if (!el) { return; }
    el.textContent = text;
    if (isError) { el.className = 'field__error'; }
  }

  function poll() {
    fetch(stateUrl, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) { throw new Error('status ' + response.status); }
        return response.json();
      })
      .then(function (state) {
        if (state.ready) {
          say('Ready. Your download is starting.');
          window.location.href = downloadUrl;
          return;
        }
        if (state.status === 'failed') {
          say(state.error || 'The document could not be rendered.', true);
          return;
        }

        elapsed += delay;
        if (elapsed >= GIVE_UP_AFTER) {
          say(
            'This is taking longer than expected. The render may still finish — ' +
            'reload this page to check.',
            true
          );
          return;
        }
        // Ease off as the wait grows.
        delay = Math.min(delay * 1.4, 5000);
        setTimeout(poll, delay);
      })
      .catch(function () {
        elapsed += delay;
        if (elapsed >= GIVE_UP_AFTER) {
          say('Lost contact with the server. Reload to check on the render.', true);
          return;
        }
        delay = Math.min(delay * 1.6, 8000);
        setTimeout(poll, delay);
      });
  }

  setTimeout(poll, delay);
})();
