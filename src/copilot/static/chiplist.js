/* Progressive enhancement for list-shaped preference fields (titles,
 * locations, ranked preferences). Each .chiplist wraps a <textarea> that
 * already holds the real one-item-per-line value the server parses
 * (dashboard.py's _parse_lines) - this script hides that textarea and
 * builds a chip editor in its place, keeping the textarea's value synced
 * on every add/remove/reorder so the form still submits the exact same
 * shape it always did. No JS -> the plain textarea still works.
 *
 * A .chiplist with data-suggest="titles|locations|companies" also gets a
 * type-ahead dropdown, backed by GET /api/suggestions (fetched once and
 * shared across every chiplist on the page, filtered client-side per
 * keystroke). Suggestions never restrict input - press Enter on unmatched
 * text and it's still added, same as any other field.
 */
(function () {
  function parseLines(text) {
    return text
      .split("\n")
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
  }

  var _suggestionsPromise = null;
  function suggestionPool(field) {
    if (!_suggestionsPromise) {
      _suggestionsPromise = fetch("/api/suggestions")
        .then(function (r) { return r.json(); })
        .catch(function () { return {}; });
    }
    return _suggestionsPromise.then(function (data) { return data[field] || []; });
  }

  function init(root) {
    var source = root.querySelector(".chiplist-source");
    var ranked = root.dataset.reorder === "true";
    var suggestField = root.dataset.suggest || null;
    var values = parseLines(source.value);
    source.hidden = true;

    var list = document.createElement(ranked ? "ol" : "div");
    list.className = ranked ? "chip-ranked" : "chip-row";

    var inputWrap = document.createElement("div");
    inputWrap.className = "chiplist-input-wrap";

    var input = document.createElement("input");
    input.type = "text";
    input.className = "chiplist-input";
    input.autocomplete = "off";
    input.placeholder = root.dataset.placeholder || "Add...";
    input.setAttribute("aria-label", root.dataset.placeholder || "Add an item");
    inputWrap.appendChild(input);

    var suggestBox = null;
    var matches = [];
    var activeIndex = -1;

    if (suggestField) {
      suggestBox = document.createElement("div");
      suggestBox.className = "chip-suggest";
      suggestBox.hidden = true;
      inputWrap.appendChild(suggestBox);
    }

    root.appendChild(list);
    root.appendChild(inputWrap);

    function sync() {
      source.value = values.join("\n");
    }

    function clearAll() {
      values.length = 0;
      render();
      sync();
    }
    // Exposed so a "Clear all" button elsewhere on the page (outside this
    // closure - see the data-clear-target wiring below) can act on this
    // specific field's chips without reaching into its internals.
    root._chiplistCount = function () { return values.length; };
    root._chiplistClear = clearAll;

    function move(from, to) {
      if (to < 0 || to >= values.length) return;
      var moved = values.splice(from, 1)[0];
      values.splice(to, 0, moved);
      render();
      sync();
    }

    function removeAt(i) {
      values.splice(i, 1);
      render();
      sync();
    }

    function render() {
      list.innerHTML = "";
      values.forEach(function (val, i) {
        var chip = document.createElement(ranked ? "li" : "span");
        chip.className = ranked ? "chip-rank-item" : "pref-chip";

        var text = document.createElement("span");
        text.className = "chip-text";
        text.textContent = val;

        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "chip-remove";
        remove.setAttribute("aria-label", "Remove " + val);
        remove.textContent = "×";
        remove.addEventListener("click", function () { removeAt(i); });

        if (ranked) {
          var handle = document.createElement("span");
          handle.className = "chip-drag";
          handle.setAttribute("aria-hidden", "true");
          handle.textContent = "⋮⋮";
          chip.draggable = true;

          var num = document.createElement("span");
          num.className = "chip-num";
          num.textContent = String(i + 1);

          var up = document.createElement("button");
          up.type = "button";
          up.className = "chip-move";
          up.setAttribute("aria-label", "Move " + val + " up");
          up.textContent = "▲";
          up.disabled = i === 0;
          up.addEventListener("click", function () { move(i, i - 1); });

          var down = document.createElement("button");
          down.type = "button";
          down.className = "chip-move";
          down.setAttribute("aria-label", "Move " + val + " down");
          down.textContent = "▼";
          down.disabled = i === values.length - 1;
          down.addEventListener("click", function () { move(i, i + 1); });

          chip.append(handle, num, text, up, down, remove);

          chip.addEventListener("dragstart", function (e) {
            e.dataTransfer.setData("text/plain", String(i));
            chip.classList.add("dragging");
          });
          chip.addEventListener("dragend", function () {
            chip.classList.remove("dragging");
          });
          chip.addEventListener("dragover", function (e) { e.preventDefault(); });
          chip.addEventListener("drop", function (e) {
            e.preventDefault();
            move(parseInt(e.dataTransfer.getData("text/plain"), 10), i);
          });
        } else {
          chip.append(text, remove);
        }
        list.appendChild(chip);
      });
    }

    function addValue(raw) {
      var val = raw.trim();
      if (!val || values.indexOf(val) !== -1) return;
      values.push(val);
      render();
      sync();
      input.value = "";
      input.focus();
    }

    function closeSuggestions() {
      if (!suggestBox) return;
      suggestBox.hidden = true;
      suggestBox.innerHTML = "";
      matches = [];
      activeIndex = -1;
    }

    function highlightActive() {
      var opts = suggestBox.querySelectorAll(".chip-suggest-opt");
      opts.forEach(function (opt, i) {
        opt.classList.toggle("active", i === activeIndex);
      });
      if (activeIndex !== -1 && opts[activeIndex]) {
        opts[activeIndex].scrollIntoView({ block: "nearest" });
      }
    }

    function updateSuggestions() {
      if (!suggestField) return;
      var query = input.value.trim().toLowerCase();
      if (!query) { closeSuggestions(); return; }
      suggestionPool(suggestField).then(function (pool) {
        if (input.value.trim().toLowerCase() !== query) return; // stale by the time it resolved
        var startsWith = [], contains = [];
        pool.forEach(function (item) {
          if (values.indexOf(item) !== -1) return;
          var lower = item.toLowerCase();
          if (lower === query) return;
          if (lower.indexOf(query) !== 0) {
            if (lower.indexOf(query) !== -1) contains.push(item);
            return;
          }
          startsWith.push(item);
        });
        matches = startsWith.concat(contains).slice(0, 8);
        activeIndex = -1;
        if (!matches.length) { closeSuggestions(); return; }
        suggestBox.innerHTML = "";
        matches.forEach(function (item) {
          var opt = document.createElement("button");
          opt.type = "button";
          opt.className = "chip-suggest-opt";
          opt.textContent = item;
          // mousedown (not click) fires before the input's blur, and
          // preventDefault keeps focus in the input so blur never fires -
          // otherwise blur's own commit-leftover-text handler would race it.
          opt.addEventListener("mousedown", function (e) {
            e.preventDefault();
            addValue(item);
            closeSuggestions();
          });
          suggestBox.appendChild(opt);
        });
        suggestBox.hidden = false;
      });
    }

    input.addEventListener("input", updateSuggestions);
    input.addEventListener("focus", updateSuggestions);

    input.addEventListener("keydown", function (e) {
      if (suggestField && !suggestBox.hidden && matches.length) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          activeIndex = (activeIndex + 1) % matches.length;
          highlightActive();
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          activeIndex = (activeIndex - 1 + matches.length) % matches.length;
          highlightActive();
          return;
        }
        if (e.key === "Escape") {
          closeSuggestions();
          return;
        }
        if (e.key === "Enter" && activeIndex !== -1) {
          e.preventDefault();
          addValue(matches[activeIndex]);
          closeSuggestions();
          return;
        }
      }
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        addValue(input.value);
        closeSuggestions();
      } else if (e.key === "Backspace" && !input.value && values.length) {
        removeAt(values.length - 1);
      }
    });
    input.addEventListener("blur", function () {
      if (input.value.trim()) addValue(input.value);
      closeSuggestions();
    });

    render();
  }

  // A plain <button data-clear-target="titles" data-clear-label="Target
  // titles"> anywhere on the page empties that field's chips - confirmed
  // first (native confirm(), no framework needed), since this is
  // irreversible until the user leaves without saving. Kept outside init()
  // since the button lives in the field-row markup, not inside .chiplist.
  function wireClearButtons() {
    document.querySelectorAll("[data-clear-target]").forEach(function (btn) {
      var source = document.querySelector('.chiplist-source[name="' + btn.dataset.clearTarget + '"]');
      var chiplist = source && source.closest(".chiplist");
      if (!chiplist) return;
      btn.addEventListener("click", function () {
        var count = chiplist._chiplistCount ? chiplist._chiplistCount() : 0;
        if (!count) return;
        var label = btn.dataset.clearLabel || "this field";
        var ok = window.confirm(
          "Clear all " + count + " " + (count === 1 ? "entry" : "entries") + " from " + label +
          "? This can't be undone unless you leave the page without saving."
        );
        if (ok) chiplist._chiplistClear();
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".chiplist").forEach(init);
    wireClearButtons();
  });
})();
