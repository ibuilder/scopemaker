/* ScopeMaker UI behaviour.
 *
 * Progressive enhancement only: every action here also works as a plain form
 * submission, so the app stays usable with JavaScript disabled or blocked --
 * which happens more often on locked-down jobsite networks than you would
 * expect.
 */
(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState !== 'loading') { fn(); }
    else { document.addEventListener('DOMContentLoaded', fn); }
  }

  function csrfToken() {
    var input = document.querySelector('input[name="csrf_token"]');
    return input ? input.value : '';
  }

  /* ------------------------------------------------------------ dismissals */
  function initDismiss() {
    document.addEventListener('click', function (event) {
      var button = event.target.closest('[data-dismiss]');
      if (!button) { return; }
      var flash = button.closest('.flash');
      if (flash) { flash.remove(); }
    });
  }

  /* --------------------------------------------------- organization switch */
  function initOrgSwitch() {
    var select = document.querySelector('[data-orgswitch]');
    if (!select) { return; }
    select.addEventListener('change', function () {
      var form = document.getElementById('orgswitch-form');
      if (!form) { return; }
      // The route takes the id in the path, so rewrite it before submitting.
      form.action = form.action.replace('PLACEHOLDER', select.value);
      form.submit();
    });
  }

  /* ------------------------------------------------------- confirm actions */
  function initConfirm() {
    document.addEventListener('submit', function (event) {
      var form = event.target;
      var message = form.getAttribute('data-confirm');
      if (message && !window.confirm(message)) {
        event.preventDefault();
      }
    });
  }

  /* ---------------------------------------------------- clause bulk select */
  function initPickerControls() {
    document.addEventListener('click', function (event) {
      var button = event.target.closest('[data-select-all], [data-select-none]');
      if (!button) { return; }
      event.preventDefault();
      var group = button.closest('.picker__group');
      if (!group) { return; }
      var checked = button.hasAttribute('data-select-all');
      group.querySelectorAll('input[type="checkbox"]').forEach(function (box) {
        box.checked = checked;
      });
      updatePickerCount(group);
    });

    document.addEventListener('change', function (event) {
      if (!event.target.matches('.picker__item input[type="checkbox"]')) { return; }
      var group = event.target.closest('.picker__group');
      if (group) { updatePickerCount(group); }
    });

    document.querySelectorAll('.picker__group').forEach(updatePickerCount);
  }

  function updatePickerCount(group) {
    var counter = group.querySelector('[data-selected-count]');
    if (!counter) { return; }
    var boxes = group.querySelectorAll('input[type="checkbox"]');
    var selected = group.querySelectorAll('input[type="checkbox"]:checked');
    counter.textContent = selected.length + ' of ' + boxes.length + ' selected';
  }

  /* ------------------------------------------------ project -> bid package */
  function initDependentPackages() {
    var projectSelect = document.querySelector('[data-project-select]');
    var packageSelect = document.querySelector('[data-package-select]');
    if (!projectSelect || !packageSelect) { return; }

    var endpoint = packageSelect.getAttribute('data-packages-url');
    if (!endpoint) { return; }

    projectSelect.addEventListener('change', function () {
      var url = endpoint + '?project_id=' + encodeURIComponent(projectSelect.value || '');
      fetch(url, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data) { return; }
          packageSelect.innerHTML = '';
          var blank = document.createElement('option');
          blank.value = '';
          blank.textContent = '-- No bid package --';
          packageSelect.appendChild(blank);
          data.bid_packages.forEach(function (pkg) {
            var option = document.createElement('option');
            option.value = pkg.id;
            option.textContent = pkg.label;
            option.setAttribute('data-division', pkg.division_code || '');
            option.setAttribute('data-trade', pkg.trade_name || '');
            packageSelect.appendChild(option);
          });
        })
        .catch(function () { /* leave the existing options in place */ });
    });

    // Selecting a package fills in its division and trade.
    packageSelect.addEventListener('change', function () {
      var option = packageSelect.options[packageSelect.selectedIndex];
      if (!option) { return; }
      var division = option.getAttribute('data-division');
      var trade = option.getAttribute('data-trade');
      var divisionField = document.querySelector('[name="division_code"]');
      var tradeField = document.querySelector('[name="trade_name"]');
      if (division && divisionField && !divisionField.value) { divisionField.value = division; }
      if (trade && tradeField && !tradeField.value) { tradeField.value = trade; }
    });
  }

  /* ------------------------------------------------------ inline item edit */
  function initInlineEdit() {
    document.addEventListener('click', function (event) {
      var toggle = event.target.closest('[data-edit-item]');
      if (toggle) {
        event.preventDefault();
        var item = toggle.closest('.item');
        if (!item) { return; }
        item.classList.toggle('is-editing');
        var field = item.querySelector('textarea');
        if (field && item.classList.contains('is-editing')) {
          field.focus();
          field.setSelectionRange(field.value.length, field.value.length);
        }
        return;
      }

      var cancel = event.target.closest('[data-cancel-edit]');
      if (cancel) {
        event.preventDefault();
        var editing = cancel.closest('.item');
        if (editing) { editing.classList.remove('is-editing'); }
      }
    });
  }

  /* --------------------------------------------------------- drag to order */
  function initReorder() {
    var lists = document.querySelectorAll('[data-reorder]');
    if (!lists.length) { return; }

    lists.forEach(function (list) {
      var dragged = null;

      list.addEventListener('dragstart', function (event) {
        var item = event.target.closest('.item');
        if (!item) { return; }
        dragged = item;
        item.classList.add('is-dragging');
        event.dataTransfer.effectAllowed = 'move';
        // Firefox requires data to be set for the drag to begin at all.
        event.dataTransfer.setData('text/plain', item.getAttribute('data-item-id') || '');
      });

      list.addEventListener('dragend', function () {
        if (dragged) { dragged.classList.remove('is-dragging'); }
        list.querySelectorAll('.is-dropzone').forEach(function (node) {
          node.classList.remove('is-dropzone');
        });
        dragged = null;
      });

      list.addEventListener('dragover', function (event) {
        if (!dragged) { return; }
        event.preventDefault();
        var target = event.target.closest('.item');
        if (!target || target === dragged) { return; }
        list.querySelectorAll('.is-dropzone').forEach(function (node) {
          node.classList.remove('is-dropzone');
        });
        target.classList.add('is-dropzone');
      });

      list.addEventListener('drop', function (event) {
        if (!dragged) { return; }
        event.preventDefault();
        var target = event.target.closest('.item');
        if (!target || target === dragged) { return; }

        var rect = target.getBoundingClientRect();
        var after = (event.clientY - rect.top) > rect.height / 2;
        target.parentNode.insertBefore(dragged, after ? target.nextSibling : target);
        target.classList.remove('is-dropzone');
        persistOrder(list);
      });
    });
  }

  /* ------------------------------------------------- keyboard reordering */
  /* Drag and drop is a pointer gesture with no keyboard equivalent, so on its
   * own it makes reordering impossible for anyone not using a mouse. These
   * buttons drive exactly the same persistOrder path.
   *
   * Focus has to be restored deliberately: moving an element in the DOM blurs
   * it, which would dump the user back at the top of the document after every
   * press and make repeated moves unusable. */
  function initKeyboardReorder() {
    document.addEventListener('click', function (event) {
      var button = event.target.closest('[data-move]');
      if (!button) { return; }
      event.preventDefault();

      var item = button.closest('.item');
      var list = button.closest('[data-reorder]');
      if (!item || !list) { return; }

      var direction = button.getAttribute('data-move');

      /* Nesting is flat in the DOM: every item in a section is a sibling, and
       * depth is carried by data-parent-id, not by the markup. So "the next
       * item" is not the next element -- it may be this item's own child.
       *
       * Swapping across depths looks like it works and saves nothing: the
       * reorder endpoint assigns positions per parent bucket, so an item's
       * order relative to a node with a different parent is not represented.
       * The row would move on screen, the page would say "Order saved", and a
       * reload would put it back. Move among true siblings instead. */
      var all = Array.prototype.filter.call(
        item.parentNode.children,
        function (node) { return node.classList.contains('item'); }
      );
      var parentId = item.getAttribute('data-parent-id') || '';
      var siblings = all.filter(function (node) {
        return (node.getAttribute('data-parent-id') || '') === parentId;
      });

      var index = siblings.indexOf(item);
      var target = direction === 'up' ? siblings[index - 1] : siblings[index + 1];

      if (!target) {
        announce(direction === 'up'
          ? 'Already the first item at this level.'
          : 'Already the last item at this level.');
        return;
      }

      /* An item's children follow it as siblings, so moving past a node means
       * moving past everything nested under it too -- otherwise the item lands
       * between a parent and its children. */
      function lastNodeOf(node) {
        var last = node;
        var at = all.indexOf(node) + 1;
        while (at < all.length &&
               (all[at].getAttribute('data-parent-id') || '') !== parentId) {
          last = all[at];
          at += 1;
        }
        return last;
      }

      var block = [item];
      var after = all.indexOf(item) + 1;
      while (after < all.length &&
             (all[after].getAttribute('data-parent-id') || '') !== parentId) {
        block.push(all[after]);
        after += 1;
      }

      var anchor = direction === 'up' ? target : lastNodeOf(target).nextSibling;
      block.forEach(function (node) {
        item.parentNode.insertBefore(node, anchor);
      });

      // The nodes moved, so the button inside this one lost focus.
      var moved = item.querySelector('[data-move="' + direction + '"]');
      if (moved) { moved.focus(); }

      announce('Moved ' + direction + ' to position ' +
               (direction === 'up' ? index : index + 2) +
               ' of ' + siblings.length + '.');
      persistOrder(list);
    });
  }

  function persistOrder(list) {
    var url = list.getAttribute('data-reorder-url');
    var sectionKey = list.getAttribute('data-section-key');
    if (!url || !sectionKey) { return; }

    var order = Array.prototype.map.call(
      list.querySelectorAll('.item[data-item-id]'),
      function (item) {
        return {
          id: item.getAttribute('data-item-id'),
          parent_id: item.getAttribute('data-parent-id') || null
        };
      }
    );

    fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
      body: JSON.stringify({ section_key: sectionKey, order: order })
    })
      .then(function (response) {
        if (!response.ok) { throw new Error('reorder failed'); }
        announce('Order saved. Reload to refresh the numbering.');
      })
      .catch(function () {
        announce('Could not save the new order. Reload and try again.', true);
      });
  }

  function announce(message, isError) {
    var region = document.getElementById('reorder-status');
    if (!region) { return; }
    region.textContent = message;
    region.className = isError ? 'field__error small' : 'muted small';
  }

  /* ------------------------------------------------------- copy to clipboard */
  function initCopy() {
    document.addEventListener('click', function (event) {
      var button = event.target.closest('[data-copy]');
      if (!button) { return; }
      event.preventDefault();
      var source = document.querySelector(button.getAttribute('data-copy'));
      if (!source) { return; }
      var text = source.textContent.trim();
      var done = function () {
        var original = button.textContent;
        button.textContent = 'Copied';
        setTimeout(function () { button.textContent = original; }, 1600);
      };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(done, function () {});
      } else {
        var area = document.createElement('textarea');
        area.value = text;
        document.body.appendChild(area);
        area.select();
        try { document.execCommand('copy'); done(); } catch (err) { /* ignore */ }
        document.body.removeChild(area);
      }
    });
  }

  ready(function () {
    initDismiss();
    initOrgSwitch();
    initConfirm();
    initPickerControls();
    initDependentPackages();
    initInlineEdit();
    initReorder();
    initKeyboardReorder();
    initCopy();
  });
})();
