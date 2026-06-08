(function () {
  const input = document.getElementById("id_solution_zip");
  const projectInput = document.getElementById("id_project_id");
  const uploader = document.getElementById("uploader");
  const fileUi = document.getElementById("fileUi");
  const orbBtn = document.getElementById("orbBtn");
  const filePill = document.getElementById("filePill");
  const fileName = document.getElementById("fileName");
  const fileSize = document.getElementById("fileSize");
  const analyzeBtn = document.getElementById("analyzeBtn");
  const clearBtn = document.getElementById("clearBtn");
  const overlay = document.getElementById("overlay");
  const overlayMessage = document.getElementById("overlayMessage");
  const progress = document.getElementById("progress");
  const fileHint = document.getElementById("fileHint");
const uploadForm = document.getElementById("uploadForm");
const cleanUploadUrl = uploadForm ? uploadForm.dataset.uploadUrl : "/";
const fileError = document.getElementById("fileError");
const projectError = document.getElementById("projectError");
const flowSummaryCard = document.getElementById("flowSummaryCard");

  const selectedJsonsContainer = document.getElementById("selectedJsonsContainer");
  const selectedFlowsSummary = document.getElementById("selectedFlowsSummary");
  const applyFlowSelectionBtn = document.getElementById("applyFlowSelectionBtn");
  const pickerError = document.getElementById("pickerError");
  const selectAllBtn = document.getElementById("selectAllBtn");
  const clearAllBtn = document.getElementById("clearAllBtn");
  const checkboxes = Array.from(document.querySelectorAll(".json-checkbox"));
  const modalTriggers = document.querySelectorAll("[data-modal-open]");
  const modals = document.querySelectorAll(".info-modal");

  const isUploadPage = !!(uploadForm && input && uploader);

  function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;

    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    modal.classList.add("is-open");
    document.body.classList.add("modal-open");
  }

  function closeModal(modal) {
    if (!modal) return;

    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
    modal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  modalTriggers.forEach(function (trigger) {
    trigger.addEventListener("click", function () {
      openModal(this.dataset.modalOpen);
    });
  });

modals.forEach(function (modal) {
  modal.addEventListener("click", function (event) {
    if (event.target === modal || event.target.matches("[data-modal-close]")) {
      closeModal(modal);
    }
  });
});

document.addEventListener("keydown", function (event) {
  if (event.key !== "Escape") return;

  document.querySelectorAll(".info-modal.is-open").forEach(function (modal) {
    closeModal(modal);
  });
});

/* ==========================================================================
   Findings table — filtros en columna + búsqueda + paginación
   ========================================================================== */
(function initFindingsPagination() {
  const table          = document.getElementById("findingDetailsTable");
  const pageSizeSelect = document.getElementById("findingsPageSize");
  const pagination     = document.getElementById("findingsPagination");
  const meta           = document.getElementById("findingsTableMeta");
  const searchInput    = document.getElementById("findingsSearch");
  const searchClear    = document.getElementById("findingsSearchClear");
  const activeFilters  = document.getElementById("findingsActiveFilters");
  const emptyFilter    = document.getElementById("findingsEmptyFilter");
  const emptyReset     = document.getElementById("findingsEmptyReset");

  if (!table || !pageSizeSelect || !pagination || !meta) return;
  const body = table.querySelector("tbody");
  if (!body) return;

  const allRows = Array.from(body.querySelectorAll("tr")).filter(
    (r) => !r.querySelector(".empty-state-cell")
  );

  if (!allRows.length) {
    meta.textContent = "Showing 0 to 0 of 0 incidents";
    pagination.hidden = true;
    return;
  }

  // Estado de filtros por columna: { severity: Set, rule: Set }
  const colFilters = { severity: new Set(), rule: new Set() };

  // ── Construir opciones dinámicas ──────────────────────────────────────────
  function buildColOptions(col, getValue) {
    const container = document.getElementById(`colOpts-${col}`);
    if (!container) return;

    const valuesSet = new Set();
    allRows.forEach((row) => {
      const v = getValue(row);
      if (v) valuesSet.add(v);
    });

    Array.from(valuesSet).sort().forEach((val) => {
      const label = document.createElement("label");
      label.className = "col-filter-option";

      const cb = document.createElement("input");
      cb.type  = "checkbox";
      cb.value = val;
      cb.addEventListener("change", () => {
        if (cb.checked) colFilters[col].add(val);
        else            colFilters[col].delete(val);
        label.classList.toggle("is-checked", cb.checked);
        currentPage = 1;
        render();
      });

      label.appendChild(cb);
      label.appendChild(document.createTextNode(val));
      container.appendChild(label);
    });
  }

  buildColOptions("severity", (row) => {
    const pill = row.querySelector(".severity-pill");
    return pill ? pill.textContent.trim() : "";
  });

  buildColOptions("rule", (row) => {
    const el = row.querySelector(".finding-rule");
    return el ? el.textContent.trim() : "";
  });

  // ── Dropdowns: abrir / cerrar ─────────────────────────────────────────────
  document.querySelectorAll(".col-filter-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const col      = btn.dataset.col;
      const dropdown = document.getElementById(`colFilter-${col}`);
      const isOpen   = !dropdown.hidden;

      // cerrar todos
      document.querySelectorAll(".col-filter-dropdown").forEach((d) => { d.hidden = true; });
      document.querySelectorAll(".col-filter-btn").forEach((b) => { b.classList.remove("is-open"); });

      if (!isOpen) {
        dropdown.hidden = false;
        btn.classList.add("is-open");
      }
    });
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".col-filter-dropdown") && !e.target.closest(".col-filter-btn")) {
      document.querySelectorAll(".col-filter-dropdown").forEach((d) => { d.hidden = true; });
      document.querySelectorAll(".col-filter-btn").forEach((b) => { b.classList.remove("is-open"); });
    }
  });

  // Botones "Clear" dentro del dropdown
  document.querySelectorAll(".col-filter-clear").forEach((btn) => {
    btn.addEventListener("click", () => {
      const col = btn.dataset.col;
      colFilters[col].clear();
      const container = document.getElementById(`colOpts-${col}`);
      if (container) {
        container.querySelectorAll("input[type=checkbox]").forEach((cb) => {
          cb.checked = false;
          cb.closest("label").classList.remove("is-checked");
        });
      }
      currentPage = 1;
      render();
    });
  });

  // ── Filtrado ──────────────────────────────────────────────────────────────
  function getSearchText() {
    return searchInput ? searchInput.value.trim().toLowerCase() : "";
  }

  function hasActiveFilters() {
    return colFilters.severity.size > 0 || colFilters.rule.size > 0 || !!getSearchText();
  }

  function getVisibleRows() {
    const text = getSearchText();
    return allRows.filter((row) => {
      if (colFilters.severity.size) {
        const pill = row.querySelector(".severity-pill");
        const val  = pill ? pill.textContent.trim() : "";
        if (!colFilters.severity.has(val)) return false;
      }
      if (colFilters.rule.size) {
        const el  = row.querySelector(".finding-rule");
        const val = el ? el.textContent.trim() : "";
        if (!colFilters.rule.has(val)) return false;
      }
      if (text && !row.textContent.toLowerCase().includes(text)) return false;
      return true;
    });
  }

  // ── Chips de filtros activos ──────────────────────────────────────────────
  function renderChips() {
    if (!activeFilters) return;
    activeFilters.innerHTML = "";

    const chips = [];

    colFilters.severity.forEach((val) => chips.push({ col: "severity", val, label: `Severity: ${val}` }));
    colFilters.rule.forEach((val)     => chips.push({ col: "rule",     val, label: `Rule: ${val}` }));

    chips.forEach(({ col, val, label }) => {
      const chip = document.createElement("div");
      chip.className = "filter-chip";
      chip.innerHTML = `${label}<button class="filter-chip__remove" aria-label="Remove filter">×</button>`;
      chip.querySelector("button").addEventListener("click", () => {
        colFilters[col].delete(val);
        // Desmarcar checkbox correspondiente
        const container = document.getElementById(`colOpts-${col}`);
        if (container) {
          container.querySelectorAll("input[type=checkbox]").forEach((cb) => {
            if (cb.value === val) { cb.checked = false; cb.closest("label").classList.remove("is-checked"); }
          });
        }
        currentPage = 1;
        render();
      });
      activeFilters.appendChild(chip);
    });

    activeFilters.hidden = chips.length === 0;
  }

  // ── Highlight de búsqueda ─────────────────────────────────────────────────
  const originalCells = allRows.map((row) =>
    Array.from(row.cells).map((cell) => cell.innerHTML)
  );

  function highlightNode(node, term) {
    if (node.nodeType !== 3 || !term) return;
    const idx = node.nodeValue.toLowerCase().indexOf(term);
    if (idx === -1) return;
    const before = document.createTextNode(node.nodeValue.slice(0, idx));
    const mark   = document.createElement("mark");
    mark.className   = "findings-highlight";
    mark.textContent = node.nodeValue.slice(idx, idx + term.length);
    const after  = document.createTextNode(node.nodeValue.slice(idx + term.length));
    const parent = node.parentNode;
    parent.insertBefore(before, node);
    parent.insertBefore(mark, node);
    parent.insertBefore(after, node);
    parent.removeChild(node);
  }

  function applyHighlights(term) {
    allRows.forEach((row, ri) => {
      Array.from(row.cells).forEach((cell, ci) => {
        cell.innerHTML = originalCells[ri][ci];
        if (!term) return;
        const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
        const nodes  = [];
        let n;
        while ((n = walker.nextNode())) nodes.push(n);
        nodes.forEach((node) => highlightNode(node, term));
      });
    });
  }

  // ── Paginación (tu estilo << < N > >>) ───────────────────────────────────
  let currentPage = 1;
  let pageSize    = Number(pageSizeSelect.value) || 10;

  function createButton(label, onClick, options = {}) {
    const btn = document.createElement("button");
    btn.type      = "button";
    btn.className = "table-page-btn";
    btn.textContent = label;
    if (options.active)   btn.classList.add("is-active");
    if (options.disabled) btn.disabled = true;
    btn.addEventListener("click", onClick);
    return btn;
  }

  function renderPagination(visibleRows) {
    const totalPages = Math.max(1, Math.ceil(visibleRows.length / pageSize));
    pagination.innerHTML = "";
    if (totalPages <= 1) { pagination.hidden = true; return; }
    pagination.hidden = false;

    const firstBtn = createButton("<<", () => { if (currentPage > 1) { currentPage = 1; render(); } }, { disabled: currentPage === 1 });
    firstBtn.setAttribute("aria-label", "Go to first page"); firstBtn.title = "First page";

    const prevBtn = createButton("<", () => { if (currentPage > 1) { currentPage--; render(); } }, { disabled: currentPage === 1 });
    prevBtn.setAttribute("aria-label", "Go to previous page"); prevBtn.title = "Previous page";

    const pageIndicator = document.createElement("span");
    pageIndicator.className = "table-page-indicator";
    pageIndicator.textContent = String(currentPage);
    pageIndicator.setAttribute("aria-label", `Page ${currentPage} of ${totalPages}`);

    const nextBtn = createButton(">", () => { if (currentPage < totalPages) { currentPage++; render(); } }, { disabled: currentPage === totalPages });
    nextBtn.setAttribute("aria-label", "Go to next page"); nextBtn.title = "Next page";

    const lastBtn = createButton(">>", () => { if (currentPage < totalPages) { currentPage = totalPages; render(); } }, { disabled: currentPage === totalPages });
    lastBtn.setAttribute("aria-label", "Go to last page"); lastBtn.title = "Last page";

    pagination.appendChild(firstBtn);
    pagination.appendChild(prevBtn);
    pagination.appendChild(pageIndicator);
    pagination.appendChild(nextBtn);
    pagination.appendChild(lastBtn);
  }

  // ── Render principal ──────────────────────────────────────────────────────
  function render() {
    const visibleRows = getVisibleRows();
    const totalPages  = Math.max(1, Math.ceil(visibleRows.length / pageSize));
    if (currentPage > totalPages) currentPage = totalPages;

    const start = (currentPage - 1) * pageSize;
    const end   = start + pageSize;

    allRows.forEach((row) => { row.style.display = "none"; });
    visibleRows.forEach((row, i) => {
      row.style.display = (i >= start && i < end) ? "" : "none";
    });

    applyHighlights(getSearchText());

    const visStart = visibleRows.length ? start + 1 : 0;
    const visEnd   = Math.min(end, visibleRows.length);
    meta.textContent = `Showing ${visStart} to ${visEnd} of ${visibleRows.length} incidents`;

    const noMatch = visibleRows.length === 0 && hasActiveFilters();
    if (emptyFilter) emptyFilter.hidden = !noMatch;
    const scroll = table.closest(".findings-scroll");
    if (scroll) scroll.style.display = noMatch ? "none" : "";

    // Icono activo en los botones de columna
    document.querySelectorAll(".col-filter-btn").forEach((btn) => {
      const col = btn.dataset.col;
      btn.classList.toggle("is-active", colFilters[col] && colFilters[col].size > 0);
    });

    if (searchClear) searchClear.hidden = !getSearchText();

    renderChips();
    renderPagination(visibleRows);
  }

  // ── Reset global ─────────────────────────────────────────────────────────
  function resetAll() {
    colFilters.severity.clear();
    colFilters.rule.clear();
    document.querySelectorAll(".col-filter-options input[type=checkbox]").forEach((cb) => {
      cb.checked = false;
      cb.closest("label").classList.remove("is-checked");
    });
    if (searchInput) searchInput.value = "";
    currentPage = 1;
    render();
  }

  // ── Eventos ───────────────────────────────────────────────────────────────
  if (searchInput) {
    searchInput.addEventListener("input", () => { currentPage = 1; render(); });
  }
  if (searchClear) {
    searchClear.addEventListener("click", () => {
      if (searchInput) searchInput.value = "";
      currentPage = 1; render(); searchInput && searchInput.focus();
    });
  }
  if (emptyReset) emptyReset.addEventListener("click", resetAll);

  pageSizeSelect.addEventListener("change", function () {
    pageSize = Number(this.value) || 10;
    currentPage = 1;
    render();
  });

  render();
})();

if (!isUploadPage) {
  return;
}
  const hasPickerState = !!selectedJsonsContainer;
  const hasPersistentUpload = !!(filePill && filePill.dataset.persistent === "true");

  function fmtBytes(bytes) {
    if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "";

    const units = ["B", "KB", "MB", "GB"];
    let value = bytes;
    let index = 0;

    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }

    return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function validateZip(file) {
    return !!file && typeof file.name === "string" && file.name.toLowerCase().endsWith(".zip");
  }

  function hasProjectId() {
    return !!projectInput && projectInput.value.trim().length > 0;
  }

  function getSelectedFile() {
    return input.files && input.files.length ? input.files[0] : null;
  }

  function getSelectedFlowCount() {
    if (!hasPickerState) return 0;
    return checkboxes.filter((cb) => cb.checked).length;
  }

  function showFileError(show, message) {
    if (!fileError) return;

    if (message) {
      fileError.textContent = message;
    }

    fileError.hidden = !show;
  }

  function showProjectError(show, message) {
    if (!projectError) return;

    if (message) {
      projectError.textContent = message;
    }

    projectError.hidden = !show;
  }

  function showPickerError(show, message) {
    if (!pickerError) return;

    if (message) {
      pickerError.textContent = message;
    }

    pickerError.hidden = !show;
  }

  function clearDisplayedFileInfo(force = false) {
    if (hasPersistentUpload && !force) {
      if (filePill) filePill.hidden = false;
      if (fileHint) fileHint.textContent = "Your file is ready to analyze";
      return;
    }

    if (fileName) fileName.textContent = "—";
    if (fileSize) fileSize.textContent = "—";
    if (filePill) filePill.hidden = true;

    if (fileHint) {
      fileHint.textContent = "Drag your .zip file here or click to browse";
    }
  }

  function syncAnalyzeState() {
    let canAnalyze = false;

    if (hasPickerState) {
      canAnalyze = hasProjectId() && getSelectedFlowCount() > 0;
    } else {
      const file = getSelectedFile();
      canAnalyze = validateZip(file);
    }

    if (analyzeBtn) {
      analyzeBtn.disabled = !canAnalyze;
    }
  }

  function resetFileUI(force = false) {
    clearDisplayedFileInfo(force);
    showFileError(false);
    syncAnalyzeState();
  }

  function setFileUI(file) {
    if (!file || !validateZip(file)) {
      clearDisplayedFileInfo();
      showFileError(true, "Please select a valid .zip file.");
      syncAnalyzeState();
      return;
    }

    if (fileName) fileName.textContent = file.name;
    if (fileSize) fileSize.textContent = fmtBytes(file.size);
    if (filePill) filePill.hidden = false;

    if (fileHint) {
      fileHint.textContent = "Your file is ready to analyze";
    }

    showFileError(false);
    syncAnalyzeState();
  }

  function openPicker() {
    input.click();
  }

  function handleInvalidZip() {
    input.value = "";

    if (filePill) {
      filePill.removeAttribute("data-persistent");
    }

    clearDisplayedFileInfo(true);
    showFileError(true, "Please select a valid .zip file.");
    syncAnalyzeState();
  }

  function discoverFlowsImmediately() {
    if (hasPickerState) return;

    const file = getSelectedFile();
    if (!validateZip(file)) return;

    // Descubrimiento automático de flujos.
    // No valida Project ID en esta fase.
    uploadForm.submit();
  }

  function updateSelectedCount() {
    const total = getSelectedFlowCount();

    if (selectedFlowsSummary) {
      selectedFlowsSummary.textContent = String(total);
    }

    if (total > 0) {
      showPickerError(false);
    }

    syncAnalyzeState();
  }

  function syncHiddenSelectedInputs() {
    if (!selectedJsonsContainer) return;

    selectedJsonsContainer.innerHTML = "";

    checkboxes
      .filter((cb) => cb.checked)
      .forEach((cb) => {
        const hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = "selected_jsons";
        hidden.value = cb.value;
        selectedJsonsContainer.appendChild(hidden);
      });
  }

  function showAnalyzeOverlay(message) {
    if (overlay) {
      overlay.classList.add("show");
      overlay.setAttribute("aria-hidden", "false");
    }

    if (overlayMessage) {
      overlayMessage.textContent = message;
    }

    if (progress) {
      let percent = 5;
      progress.style.width = percent + "%";

      const timer = window.setInterval(function () {
        percent = Math.min(95, percent + 7);
        progress.style.width = percent + "%";
      }, 250);

      window.setTimeout(function () {
        window.clearInterval(timer);
      }, 6000);
    }
  }

  if (fileUi) {
    fileUi.addEventListener("click", function (event) {
      event.preventDefault();
      openPicker();
    });
  }

  if (orbBtn) {
    orbBtn.addEventListener("click", function (event) {
      event.preventDefault();
      openPicker();
    });
  }

  input.addEventListener("change", function () {
    const file = getSelectedFile();

    if (!file) {
      resetFileUI();
      return;
    }

    if (!validateZip(file)) {
      handleInvalidZip();
      return;
    }

    setFileUI(file);

    // En cuanto se selecciona el ZIP, descubrimos los flujos automáticamente.
    if (!hasPickerState) {
      window.setTimeout(function () {
        discoverFlowsImmediately();
      }, 120);
    }
  });

if (clearBtn) {
  clearBtn.setAttribute("type", "button");
  clearBtn.addEventListener("click", function (event) {
    event.preventDefault();
    event.stopPropagation();

    if (filePill) {
      filePill.removeAttribute("data-persistent");
    }

    input.value = "";

    showFileError(false);
    showProjectError(false);
    showPickerError(false);

    if (selectedJsonsContainer) {
      selectedJsonsContainer.innerHTML = "";
    }

    if (flowSummaryCard) {
      flowSummaryCard.hidden = true;
    }

    clearDisplayedFileInfo(true);

    if (analyzeBtn) {
      analyzeBtn.disabled = true;
    }

    if (hasPickerState) {
      window.location.href = cleanUploadUrl;
      return;
    }

    resetFileUI(true);
  });
}

  if (projectInput) {
    projectInput.addEventListener("input", function () {
      if (hasProjectId()) {
        showProjectError(false);
      }

      syncAnalyzeState();
    });

    projectInput.addEventListener("blur", function () {
      if (hasPickerState && !hasProjectId()) {
        showProjectError(true, "Please enter a Project ID before continuing.");
      }
    });
  }

  ["dragenter", "dragover"].forEach(function (evt) {
    uploader.addEventListener(evt, function (event) {
      event.preventDefault();
      event.stopPropagation();
      uploader.classList.add("is-dragover");
    });
  });

  ["dragleave", "drop"].forEach(function (evt) {
    uploader.addEventListener(evt, function (event) {
      event.preventDefault();
      event.stopPropagation();
      uploader.classList.remove("is-dragover");
    });
  });

  uploader.addEventListener("drop", function (event) {
    const dataTransfer = event.dataTransfer;

    if (!dataTransfer || !dataTransfer.files || dataTransfer.files.length === 0) {
      return;
    }

    const file = dataTransfer.files[0];

    if (!validateZip(file)) {
      handleInvalidZip();
      return;
    }

    try {
      input.files = dataTransfer.files;
      setFileUI(file);

      if (!hasPickerState) {
        window.setTimeout(function () {
          discoverFlowsImmediately();
        }, 120);
      }
    } catch (error) {
      setFileUI(file);
    }
  });

  if (selectAllBtn) {
    selectAllBtn.addEventListener("click", function () {
      checkboxes.forEach((cb) => {
        cb.checked = true;
      });
      updateSelectedCount();
    });
  }

  if (clearAllBtn) {
    clearAllBtn.addEventListener("click", function () {
      checkboxes.forEach((cb) => {
        cb.checked = false;
      });
      updateSelectedCount();
    });
  }

  checkboxes.forEach((checkbox) => {
    checkbox.addEventListener("change", updateSelectedCount);
  });

  if (applyFlowSelectionBtn) {
    applyFlowSelectionBtn.addEventListener("click", function () {
      const total = getSelectedFlowCount();

      if (total === 0) {
        showPickerError(true, "Select at least one flow.");
        return;
      }

      syncHiddenSelectedInputs();
      updateSelectedCount();

      document.querySelectorAll(".info-modal.is-open").forEach(function (modal) {
        closeModal(modal);
      });
    });
  }

  uploadForm.addEventListener("submit", function (event) {
    // Si todavía no existe el estado del picker, este submit es el de descubrimiento automático
    // o el submit manual para cargar el ZIP.
    if (!hasPickerState) {
      const file = getSelectedFile();

      if (!validateZip(file)) {
        event.preventDefault();
        showFileError(true, "Please select a valid .zip file.");
        syncAnalyzeState();
        return;
      }

      showFileError(false);
      return;
    }

    // Si ya existen flujos descubiertos, este submit es el análisis final.
    const projectOk = hasProjectId();
    const selectedCount = getSelectedFlowCount();

    if (!projectOk) {
      event.preventDefault();
      showProjectError(true, "Please enter a Project ID before continuing.");
    } else {
      showProjectError(false);
    }

    if (selectedCount === 0) {
      event.preventDefault();
      showPickerError(true, "Select at least one flow.");
    } else {
      showPickerError(false);
    }

    if (!projectOk || selectedCount === 0) {
      syncAnalyzeState();
      return;
    }

    syncHiddenSelectedInputs();
    showAnalyzeOverlay("Analyzing selected flows and preparing the Excel report…");
  });

  if (hasPersistentUpload) {
    if (filePill) filePill.hidden = false;
    if (fileHint) fileHint.textContent = "Your file is ready to analyze";
    showFileError(false);
  } else {
    resetFileUI();
  }

  showProjectError(false);

  if (hasPickerState) {
    syncHiddenSelectedInputs();
    updateSelectedCount();
  } else {
    syncAnalyzeState();
  }
}

)();
