"use strict";

const root = document.getElementById("app");
const state = {
  shop: null,
  shopSlug: "",
  draftToken: "",
  draftId: "",
  documents: [],
  quote: null,
  uploadXhr: null,
  customerStage: "upload",
  placedOrder: null,
  trackingToken: "",
  owner: null,
  csrf: "",
  ownerView: "overview",
  ownerOrders: [],
  ownerFilters: { search: "", status: "", payment_status: "", from: "", to: "", offset: 0 },
  selectedOrder: null,
  settings: null,
  agents: [],
  settingsTab: "profile",
  settingError: "",
  notifications: [],
};

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const money = (paise) => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 }).format((Number(paise) || 0) / 100);
const dateTime = (value) => value ? new Date(value).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "—";
const pretty = (value) => String(value || "—").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
const sizeLabel = (bytes) => bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;

function toast(message, kind = "") {
  let region = document.querySelector(".toast-region");
  if (!region) {
    region = document.createElement("div");
    region.className = "toast-region";
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    document.body.append(region);
  }
  const item = document.createElement("div");
  item.className = `toast ${kind}`;
  item.textContent = message;
  region.append(item);
  setTimeout(() => item.remove(), 4200);
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (state.owner?.shop && path.startsWith("/api/admin/")) headers.set("X-Shop-Slug", state.owner.shop.slug);
  if (state.csrf && ["POST", "PUT", "PATCH", "DELETE"].includes(method)
      && (path.startsWith("/api/admin/") || path.startsWith("/api/super-admin/") || path.startsWith("/api/auth/"))) {
    headers.set("X-CSRF-Token", state.csrf);
  }
  const response = await fetch(path, {
    method, headers, credentials: "same-origin",
    body: options.body === undefined ? undefined : options.body instanceof FormData ? options.body : JSON.stringify(options.body),
    cache: "no-store",
  });
  const isJson = response.headers.get("content-type")?.includes("application/json");
  const result = isJson ? await response.json() : await response.text();
  if (!response.ok) throw new Error(result?.error?.message || `Request failed (${response.status}).`);
  return result;
}

function brandMark(letter = "S") {
  return `<span class="wordmark-mark" aria-hidden="true">${esc(letter.slice(0, 1).toUpperCase())}</span>`;
}

function renderNotice(text, kind = "") {
  return text ? `<div class="notice ${kind}" role="status">${esc(text)}</div>` : "";
}

function renderSteps(active) {
  const steps = [["upload", "Documents"], ["configure", "Print setup"], ["checkout", "Review & pay"], ["ticket", "Ticket"]];
  const current = steps.findIndex(([key]) => key === active);
  return `<nav class="steps" aria-label="Order progress">${steps.map(([key, label], index) =>
    `${index ? '<span class="step-line" aria-hidden="true"></span>' : ""}<span class="step ${index === current ? "active" : index < current ? "done" : ""}"><span class="step-num">${index < current ? "✓" : index + 1}</span>${esc(label)}</span>`).join("")}</nav>`;
}

function customerHeader() {
  const shop = state.shop;
  return `<div class="customer-topbar"><div class="wordmark">${brandMark(shop?.name || "S")}<span>${esc(shop?.name || "Print shop")}</span></div><span class="shop-status ${shop?.available ? "" : "closed"}">${shop?.available ? "Accepting orders" : "Not accepting orders"}</span></div>`;
}

function renderCustomerShell(content, stage = state.customerStage) {
  root.innerHTML = `<main class="customer-page"><div class="customer-wrap">${customerHeader()}
    <section class="customer-hero"><div><p class="eyebrow">Print orders, made simple</p><h1>${esc(state.shop?.name || "Your local print shop")}</h1><p>${esc(state.shop?.profile?.instructions || "Send your documents ahead. Choose how each one should print, then collect it from the counter.")}</p></div><div class="hero-mark" aria-hidden="true">${esc((state.shop?.name || "P").slice(0, 1).toUpperCase())}</div></section>
    ${renderSteps(stage)}${content}</div></main>`;
}

function customerAside() {
  const shop = state.shop || {};
  const profile = shop.profile || {};
  const breakdown = state.quote;
  const total = breakdown?.needs_review ? `<div class="notice">A document page count needs shop review. You will see the final price before payment.</div>` : breakdown ? `<div class="summary-line"><span>Printing</span><strong>${money(breakdown.items.reduce((sum, item) => sum + item.print_paise, 0))}</strong></div>
    <div class="summary-line"><span>Additional services</span><strong>${money(breakdown.items.reduce((sum, item) => sum + item.add_ons_paise, 0))}</strong></div>
    ${breakdown.minimum_adjustment_paise ? `<div class="summary-line"><span>Minimum order adjustment</span><strong>${money(breakdown.minimum_adjustment_paise)}</strong></div>` : ""}
    ${breakdown.discount_paise ? `<div class="summary-line"><span>Shop discount</span><strong>−${money(breakdown.discount_paise)}</strong></div>` : ""}
    ${breakdown.service_fee_paise ? `<div class="summary-line"><span>Service fee</span><strong>${money(breakdown.service_fee_paise)}</strong></div>` : ""}
    <div class="summary-line"><span>Tax</span><strong>${money(breakdown.tax_paise)}</strong></div><div class="summary-total"><span>Total</span><span>${money(breakdown.total_paise)}</span></div>`
    : `<div class="summary-line"><span>Upload documents to see your price</span></div>`;
  return `<aside class="customer-aside"><section class="surface sticky-summary"><h2 class="summary-title">Order summary</h2>${total}</section>
    <section class="surface panel"><div class="panel-head"><div><h3>Shop details</h3><p>Need help with your order?</p></div></div>
      ${profile.address ? `<div class="info-line"><span class="info-dot">⌖</span><span>${esc(profile.address)}</span></div>` : ""}
      ${profile.phone ? `<div class="info-line"><span class="info-dot">☎</span><a href="tel:${esc(profile.phone)}">${esc(profile.phone)}</a></div>` : ""}
      ${profile.estimated_minutes ? `<div class="info-line"><span class="info-dot">◷</span><span>Estimated wait: about ${esc(profile.estimated_minutes)} minutes</span></div>` : ""}
      <div class="info-line"><span class="info-dot">✓</span><span>PDF and image uploads are checked before printing.</span></div>
    </section></aside>`;
}

function renderShopPicker(shops) {
  root.innerHTML = `<main class="customer-page"><div class="customer-wrap"><div class="customer-topbar"><div class="wordmark">${brandMark("S")}<span>Choose a print shop</span></div></div><section class="surface panel"><div class="panel-head"><div><h1>Where would you like to print?</h1><p>Select the shop shown on the QR code or shared order link.</p></div></div><div class="file-list">${shops.map((shop) => `<button class="btn secondary full" data-shop-select="${esc(shop.slug)}">${esc(shop.name)} <span aria-hidden="true">→</span></button>`).join("")}</div></section></div></main>`;
  root.querySelectorAll("[data-shop-select]").forEach((button) => button.addEventListener("click", () => {
    location.href = `/?shop=${encodeURIComponent(button.dataset.shopSelect)}`;
  }));
}

async function loadCustomerShop() {
  const params = new URLSearchParams(location.search);
  const slug = params.get("shop") || "";
  if (slug) {
    const result = await api(`/api/public/shops/${encodeURIComponent(slug)}`);
    state.shop = result.shop;
    state.shopSlug = result.shop.slug;
    return;
  }
  const result = await api("/api/public/default-shop");
  if (result.shop) {
    location.replace(`/?shop=${encodeURIComponent(result.shop.slug)}`);
    return;
  }
  if (result.shops?.length) return renderShopPicker(result.shops);
  throw new Error("No shop has been initialized yet.");
}

async function startCustomer() {
  try {
    await loadCustomerShop();
    if (!state.shop) return; // A shop picker has already been rendered.
    await restoreDraft();
    renderCustomerOrder();
  } catch (error) {
    root.innerHTML = `<main class="customer-page"><div class="customer-wrap"><section class="surface panel"><h1>Shop unavailable</h1>${renderNotice(error.message, "error")}<a class="btn secondary" href="/">Try again</a></section></div></main>`;
  }
}

async function ensureDraft() {
  if (state.draftToken) return;
  const response = await api(`/api/public/shops/${encodeURIComponent(state.shopSlug)}/drafts`, {
    method: "POST", headers: { "Idempotency-Key": `draft-${crypto.randomUUID()}` }, body: {},
  });
  state.draftToken = response.token;
  state.draftId = response.draft.id;
  sessionStorage.setItem(`smx-draft:${state.shopSlug}`, JSON.stringify({ token: state.draftToken, id: state.draftId }));
}

async function restoreDraft() {
  try {
    const value = JSON.parse(sessionStorage.getItem(`smx-draft:${state.shopSlug}`) || "null");
    if (value?.token && value?.id) {
      state.draftToken = value.token; state.draftId = value.id;
      const result = await api(`/api/public/shops/${encodeURIComponent(state.shopSlug)}/documents`, {
        headers: { "X-Order-Draft": state.draftToken },
      });
      state.documents = result.documents || [];
      await refreshQuote();
    }
  } catch {
    sessionStorage.removeItem(`smx-draft:${state.shopSlug}`);
    state.draftToken = ""; state.draftId = ""; state.documents = []; state.quote = null;
  }
}

function uploadFiles(files) {
  const chosen = Array.from(files || []);
  if (!chosen.length || state.uploadXhr) return;
  if (chosen.some((file) => file.size > state.shop.max_file_bytes)) {
    toast(`Each file must be at most ${sizeLabel(state.shop.max_file_bytes)}.`, "error");
    return;
  }
  if (state.documents.length + chosen.length > state.shop.max_files) {
    toast(`This shop accepts up to ${state.shop.max_files} documents per order.`, "error");
    return;
  }
  void ensureDraft().then(() => new Promise((resolve, reject) => {
    const form = new FormData();
    chosen.forEach((file) => form.append("files", file, file.name));
    const xhr = new XMLHttpRequest();
    state.uploadXhr = xhr;
    const uploadZone = document.querySelector(".upload-zone");
    if (uploadZone) uploadZone.innerHTML = `<span class="upload-icon" aria-hidden="true">↑</span><h3>Uploading documents</h3><p>Keep this page open while files are checked.</p><progress class="upload-progress" max="100" value="0" aria-label="Upload progress"></progress><button class="btn secondary small" type="button" data-cancel-upload>Cancel upload</button>`;
    xhr.open("POST", `/api/public/shops/${encodeURIComponent(state.shopSlug)}/documents`);
    xhr.setRequestHeader("X-Order-Draft", state.draftToken);
    xhr.upload.onprogress = (event) => {
      const progress = document.querySelector(".upload-progress");
      if (progress && event.lengthComputable) progress.value = Math.round((event.loaded / event.total) * 100);
    };
    xhr.onload = () => {
      state.uploadXhr = null;
      let response;
      try { response = JSON.parse(xhr.responseText); } catch { response = {}; }
      if (xhr.status < 200 || xhr.status >= 300) reject(new Error(response?.error?.message || "Upload failed."));
      else resolve(response);
    };
    xhr.onerror = () => { state.uploadXhr = null; reject(new Error("Upload interrupted. Check your connection and try again.")); };
    xhr.onabort = () => { state.uploadXhr = null; reject(new Error("Upload cancelled.")); };
    document.querySelector("[data-cancel-upload]")?.addEventListener("click", () => xhr.abort());
    xhr.send(form);
  })).then(async (result) => {
    state.documents.push(...result.documents.map((doc, index) => ({
      ...doc, localPreviewUrl: URL.createObjectURL(chosen[index]),
    })));
    state.customerStage = "configure";
    await refreshQuote();
    renderCustomerOrder();
  }).catch((error) => {
    if (error.message !== "Upload cancelled.") toast(error.message, "error");
    renderCustomerOrder();
  });
}

async function refreshQuote() {
  if (!state.documents.length || !state.draftToken) { state.quote = null; return; }
  try {
    const result = await api(`/api/public/shops/${encodeURIComponent(state.shopSlug)}/quote`, {
      method: "POST", headers: { "X-Order-Draft": state.draftToken }, body: {},
    });
    state.quote = result.quote;
    return result.quote;
  } catch (error) {
    state.quote = null;
    state.quoteError = error.message;
    return null;
  }
}

function formatRateDisplay(paise) { return (Number(paise || 0) / 100).toFixed(2); }

function renderDocumentCard(doc) {
  const settings = doc.settings || {};
  const paperOptions = (state.shop.paper_sizes || []).map((size) => `<option value="${esc(size)}" ${settings.paper_size === size ? "selected" : ""}>${esc(size)}</option>`).join("");
  const addOns = (state.shop.add_ons || []).map((service) => `<label class="checkbox-line"><input type="checkbox" data-doc-addon="${esc(service)}" data-doc-id="${esc(doc.id)}" ${settings.add_ons?.includes(service) ? "checked" : ""}> ${esc(pretty(service))}</label>`).join("");
  const pages = doc.page_count ? `${doc.page_count} page${doc.page_count === 1 ? "" : "s"}` : "Page count needs shop review";
  const line = state.quote?.items.find((item) => item.document_id === doc.id);
  return `<article class="file-card" data-document="${esc(doc.id)}"><div class="file-head"><span class="file-type">${esc(doc.mime_type?.split("/").pop()?.toUpperCase() || "FILE")}</span><div class="file-head-main"><div class="file-name" title="${esc(doc.original_name)}">${esc(doc.original_name)}</div><div class="file-size">${sizeLabel(doc.size_bytes)} · ${esc(pages)}</div></div><button class="icon-btn" type="button" data-remove-doc="${esc(doc.id)}" aria-label="Remove ${esc(doc.original_name)}">×</button></div>
    ${doc.needs_review ? renderNotice("Page count could not be verified. The shop must check this document before pricing.", "error") : ""}
    <div class="file-settings">
      <div class="field"><label for="color-${esc(doc.id)}">Color</label><select id="color-${esc(doc.id)}" data-doc-setting="color" data-doc-id="${esc(doc.id)}"><option value="bw" ${settings.color === "bw" ? "selected" : ""}>Black & white</option><option value="color" ${settings.color === "color" ? "selected" : ""}>Color</option></select></div>
      <div class="field"><label for="sides-${esc(doc.id)}">Sides</label><select id="sides-${esc(doc.id)}" data-doc-setting="sides" data-doc-id="${esc(doc.id)}"><option value="single" ${settings.sides === "single" ? "selected" : ""}>Single side</option><option value="double" ${settings.sides === "double" ? "selected" : ""}>Double side</option></select></div>
      <div class="field"><label for="paper-${esc(doc.id)}">Paper</label><select id="paper-${esc(doc.id)}" data-doc-setting="paper_size" data-doc-id="${esc(doc.id)}">${paperOptions}</select></div>
      <div class="field"><label for="copies-${esc(doc.id)}">Copies</label><input id="copies-${esc(doc.id)}" data-doc-setting="copies" data-doc-id="${esc(doc.id)}" type="number" min="1" max="20" value="${esc(settings.copies || 1)}"></div>
      <div class="field"><label for="pages-${esc(doc.id)}">Pages</label><input id="pages-${esc(doc.id)}" data-doc-setting="page_selection" data-doc-id="${esc(doc.id)}" value="${esc(settings.page_selection || "all")}" placeholder="all or 1-5, 8" ${doc.page_count ? `maxlength="${doc.page_count * 8}"` : ""}></div>
      <div class="field"><label for="orientation-${esc(doc.id)}">Orientation</label><select id="orientation-${esc(doc.id)}" data-doc-setting="orientation" data-doc-id="${esc(doc.id)}"><option value="auto" ${settings.orientation === "auto" ? "selected" : ""}>Auto</option><option value="portrait" ${settings.orientation === "portrait" ? "selected" : ""}>Portrait</option><option value="landscape" ${settings.orientation === "landscape" ? "selected" : ""}>Landscape</option></select></div>
    </div>
    ${addOns ? `<div class="file-extras">${addOns}</div>` : ""}
    <div class="file-footer"><span class="micro">${line?.needs_review ? "Price after shop review" : line ? `${line.print_units} print units` : "Set options for this file"}</span><div class="detail-actions">${doc.localPreviewUrl ? `<button class="btn secondary small" type="button" data-preview-upload="${esc(doc.id)}">Preview</button>` : ""}<span class="file-line-price">${line?.needs_review ? "To confirm" : line ? money(line.total_paise) : "—"}</span></div></div></article>`;
}

function renderCustomerOrder() {
  if (!state.shop) return;
  if (state.placedOrder) return renderCustomerTicket();
  const upload = `<section class="surface panel"><div class="panel-head"><div><h2>1. Add your documents</h2><p>Upload one or more files. PDF gives the clearest page estimate.</p></div>${state.documents.length ? `<span class="status-badge">${state.documents.length} file${state.documents.length === 1 ? "" : "s"}</span>` : ""}</div>
    ${state.shop.available ? "" : renderNotice("The shop has temporarily paused new orders. Please try again later.", "error")}
    ${state.shop.pricing_ready ? "" : renderNotice("This shop has not finished configuring print prices yet. Please ask at the counter.", "error")}
    <div class="upload-zone" role="group" aria-label="Upload print documents" tabindex="0"><span class="upload-icon" aria-hidden="true">↑</span><h3>Choose files from your phone or computer</h3><p>PDF, JPG, JPEG, PNG${state.shop.formats.includes("docx") ? ", DOCX" : ""} · up to ${sizeLabel(state.shop.max_file_bytes)} each</p><button class="btn secondary" type="button" data-browse>Browse documents</button><input id="file-picker" class="sr-only" type="file" accept="${(state.shop.formats || []).map((format) => `.${format}`).join(",")}" multiple><div class="format-list">${(state.shop.formats || []).map((format) => `<span class="format-pill">${esc(format.toUpperCase())}</span>`).join("")}</div></div>
    ${state.documents.length ? `<div class="file-list spaced">${state.documents.map(renderDocumentCard).join("")}</div>` : ""}
    ${state.quoteError && state.documents.length ? renderNotice(state.quoteError, "error") : ""}
    <div class="action-row"><span class="micro">No account needed. Your files stay private to this shop.</span><button class="btn accent" type="button" data-go-checkout ${!state.documents.length || !state.quote || !state.shop.available ? "disabled" : ""}>Review order <span aria-hidden="true">→</span></button></div>
  </section>`;
  const summary = `<div class="customer-main"><div class="customer-content">${upload}</div>${customerAside()}</div>`;
  renderCustomerShell(summary, state.customerStage === "checkout" ? "checkout" : state.customerStage);
  bindCustomerUpload();
}

function bindCustomerUpload() {
  const zone = document.querySelector(".upload-zone");
  if (!zone) return;
  const picker = zone.querySelector("#file-picker");
  zone.querySelector("[data-browse]")?.addEventListener("click", () => picker.click());
  picker?.addEventListener("change", () => { uploadFiles(picker.files); picker.value = ""; });
  zone.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && !event.target.matches("button,input")) { event.preventDefault(); picker.click(); }
  });
  zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("dragging"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
  zone.addEventListener("drop", (event) => { event.preventDefault(); zone.classList.remove("dragging"); uploadFiles(event.dataTransfer.files); });
  document.querySelectorAll("[data-doc-setting]").forEach((input) => {
    input.addEventListener("change", () => saveDocumentSetting(input.dataset.docId, input.dataset.docSetting, input.type === "number" ? Number(input.value) : input.value));
  });
  document.querySelectorAll("[data-doc-addon]").forEach((input) => input.addEventListener("change", () => {
    const doc = state.documents.find((entry) => entry.id === input.dataset.docId);
    const addOns = new Set(doc.settings.add_ons || []);
    input.checked ? addOns.add(input.dataset.docAddon) : addOns.delete(input.dataset.docAddon);
    void saveDocumentSetting(doc.id, "add_ons", [...addOns]);
  }));
  document.querySelectorAll("[data-remove-doc]").forEach((button) => button.addEventListener("click", () => void removeDocument(button.dataset.removeDoc)));
  document.querySelectorAll("[data-preview-upload]").forEach((button) => button.addEventListener("click", () => openUploadedPreview(button.dataset.previewUpload)));
  document.querySelector("[data-go-checkout]")?.addEventListener("click", () => {
    state.customerStage = "checkout";
    void renderCheckout();
  });
}

async function saveDocumentSetting(id, key, value) {
  const doc = state.documents.find((entry) => entry.id === id);
  if (!doc) return;
  const settings = { ...doc.settings, [key]: value };
  try {
    const result = await api(`/api/public/shops/${encodeURIComponent(state.shopSlug)}/documents/${encodeURIComponent(id)}/settings`, {
      method: "PATCH", headers: { "X-Order-Draft": state.draftToken }, body: { settings },
    });
    doc.settings = result.settings;
    state.quote = result.quote;
    state.quoteError = "";
    renderCustomerOrder();
  } catch (error) {
    state.quoteError = error.message;
    toast(error.message, "error");
    renderCustomerOrder();
  }
}

async function removeDocument(id) {
  try {
    await api(`/api/public/shops/${encodeURIComponent(state.shopSlug)}/documents/${encodeURIComponent(id)}`, {
      method: "DELETE", headers: { "X-Order-Draft": state.draftToken },
    });
    const removed = state.documents.find((document) => document.id === id);
    if (removed?.localPreviewUrl) URL.revokeObjectURL(removed.localPreviewUrl);
    state.documents = state.documents.filter((document) => document.id !== id);
    await refreshQuote();
    renderCustomerOrder();
  } catch (error) { toast(error.message, "error"); }
}

function openUploadedPreview(id) {
  const doc = state.documents.find((item) => item.id === id);
  if (!doc?.localPreviewUrl) return;
  const preview = doc.mime_type === "application/pdf"
    ? `<iframe class="document-preview" title="Preview ${esc(doc.original_name)}" src="${esc(doc.localPreviewUrl)}"></iframe>`
    : `<img class="image-preview" src="${esc(doc.localPreviewUrl)}" alt="Preview of ${esc(doc.original_name)}">`;
  showModal(`Preview ${doc.original_name}`, preview);
}

async function renderCheckout() {
  const quote = state.quote || await refreshQuote();
  if (!quote) { state.customerStage = "configure"; renderCustomerOrder(); return; }
  const paymentMethods = quote.needs_review ? renderNotice("The shop will check the page count and confirm your final price. No payment is collected yet.") : [
    state.shop.cash_allowed ? `<label class="payment-choice"><input type="radio" name="payment-method" value="counter" checked><span><strong>Pay at the counter</strong><small>Shop staff will verify payment when you arrive.</small></span></label>` : "",
    state.shop.online_payment_available ? `<label class="payment-choice"><input type="radio" name="payment-method" value="razorpay"><span><strong>Pay online</strong><small>UPI, cards, and other methods enabled for the shop.</small></span></label>` : "",
  ].filter(Boolean).join("");
  const content = `<div class="customer-main"><div class="customer-content"><section class="surface panel"><div class="panel-head"><div><p class="eyebrow">Final check</p><h2>Review and place your order</h2><p>Prices are calculated by the shop server using its current rate card.</p></div></div>
      <div class="file-list">${quote.items.map((item) => `<div class="file-card"><div class="file-head"><span class="file-type">PDF</span><div class="file-head-main"><div class="file-name">${esc(item.name)}</div><div class="file-size">${item.pages ?? "Page count to confirm"}${item.pages === null ? "" : " pages"} · ${item.copies} copies · ${esc(item.paper_size)} · ${esc(item.color === "bw" ? "Black & white" : "Color")} · ${esc(item.sides)}-side</div></div><strong>${item.needs_review ? "To confirm" : money(item.total_paise)}</strong></div></div>`).join("")}</div>
      <div class="ticket-divider"></div><div class="field-row"><div class="field"><label for="customer-name">Name <span class="micro">(optional)</span></label><input id="customer-name" autocomplete="name" maxlength="120" placeholder="Name for your ticket"></div><div class="field"><label for="customer-phone">Mobile <span class="micro">(optional)</span></label><input id="customer-phone" type="tel" autocomplete="tel" maxlength="32" placeholder="For order updates"></div></div>
      <div class="field spaced"><span class="field-label">${quote.needs_review ? "Before payment" : "Payment method"}</span>${paymentMethods || renderNotice("No payment method is currently enabled for this shop.", "error")}</div>
      <div class="action-row"><button class="btn secondary" type="button" data-back-upload>← Edit documents</button><button class="btn accent" type="button" data-place-order ${!quote.needs_review && !paymentMethods ? "disabled" : ""}>${quote.needs_review ? "Request price review" : "Place order"} <span aria-hidden="true">→</span></button></div>
    </section></div>${customerAside()}</div>`;
  renderCustomerShell(content, "checkout");
  document.querySelector("[data-back-upload]")?.addEventListener("click", () => { state.customerStage = "configure"; renderCustomerOrder(); });
  document.querySelector("[data-place-order]")?.addEventListener("click", () => void placeOrder());
}

async function placeOrder() {
  const button = document.querySelector("[data-place-order]");
  if (!button) return;
  button.disabled = true;
  button.textContent = "Placing order…";
  const paymentMethod = state.quote?.needs_review ? "review" : document.querySelector("[name=payment-method]:checked")?.value;
  try {
    const response = await api(`/api/public/shops/${encodeURIComponent(state.shopSlug)}/orders`, {
      method: "POST", headers: { "X-Order-Draft": state.draftToken, "Idempotency-Key": `order-${state.draftId}` },
      body: { customer_name: document.getElementById("customer-name")?.value || "", customer_phone: document.getElementById("customer-phone")?.value || "", payment_method: paymentMethod },
    });
    state.placedOrder = response.order;
    state.trackingToken = response.tracking_token;
    state.customerStage = "ticket";
    sessionStorage.removeItem(`smx-draft:${state.shopSlug}`);
    if (paymentMethod === "razorpay") {
      renderCustomerTicket("Preparing secure payment…");
      await launchRazorpay(response.order);
    } else renderCustomerTicket();
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
    button.textContent = "Place order →";
  }
}

async function loadRazorpayScript() {
  if (window.Razorpay) return;
  await new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://checkout.razorpay.com/v1/checkout.js";
    script.onload = resolve;
    script.onerror = () => reject(new Error("Secure checkout could not load. You can pay at the counter."));
    document.head.append(script);
  });
}

async function launchRazorpay(order) {
  try {
    await loadRazorpayScript();
    const response = await api(`/api/public/shops/${encodeURIComponent(state.shopSlug)}/orders/payment`, {
      method: "POST", headers: { "X-Tracking-Token": state.trackingToken, "Idempotency-Key": `payment-${crypto.randomUUID()}` },
      body: { order_id: order.id },
    });
    const payment = response.payment;
    const checkout = new window.Razorpay({
      key: payment.key_id, amount: payment.amount, currency: payment.currency, name: payment.shop_name,
      description: `Print order ${payment.ticket}`, order_id: payment.razorpay_order_id,
      handler: async (result) => {
        try {
          await api("/api/payments/razorpay/verify", {
            method: "POST", headers: { "X-Tracking-Token": state.trackingToken },
            body: { order_id: order.id, shop: state.shopSlug, razorpay_order_id: result.razorpay_order_id,
                    razorpay_payment_id: result.razorpay_payment_id, razorpay_signature: result.razorpay_signature },
          });
          state.placedOrder.payment_status = "paid";
          state.placedOrder.status = "confirmed";
          toast("Payment verified by the server.", "success");
          renderCustomerTicket();
        } catch (error) {
          toast(error.message, "error");
          renderCustomerTicket("Payment was not verified. The order remains unpaid until the server confirms it.");
        }
      },
      modal: { ondismiss: () => { renderCustomerTicket("Checkout was closed. Your ticket is saved; payment is still pending."); } },
      prefill: { name: document.getElementById("customer-name")?.value || "", contact: document.getElementById("customer-phone")?.value || "" },
      theme: { color: "#183b35" },
    });
    checkout.on("payment.failed", () => renderCustomerTicket("Payment did not complete. Your ticket remains available for retry or counter payment."));
    checkout.open();
  } catch (error) {
    toast(error.message, "error");
    renderCustomerTicket(error.message);
  }
}

const STATUS_COPY = {
  awaiting_payment: "Your price is confirmed. Complete payment before the shop starts printing.", payment_verification: "The shop is checking page counts and will confirm your price before payment.",
  confirmed: "Your order is confirmed and waiting for the shop.", queued: "Your documents are in the print queue.",
  preparing_documents: "The shop is preparing your documents.", ready_for_printing: "Your order is ready to print.",
  printing: "Your documents are being printed.", partially_completed: "Some documents are printed; the shop is finishing the rest.",
  ready_for_collection: "Your order is ready for collection.", completed: "Your print order is complete.",
  cancelled: "This order was cancelled.", failed: "The shop hit a printing problem and is reviewing the order.",
};

function renderCustomerTicket(note = "") {
  const order = state.placedOrder;
  if (!order) return;
  const trackingUrl = `${location.origin}/track?shop=${encodeURIComponent(state.shopSlug)}&ticket=${encodeURIComponent(order.ticket)}#${state.trackingToken}`;
  const message = STATUS_COPY[order.status] || STATUS_COPY.awaiting_payment;
  const summaryItems = (order.items || []).map((item) => `<div class="summary-line"><span>${esc(item.name)} · ${item.pages ?? "Pages to confirm"}${item.pages == null ? "" : ` pages × ${item.copies}`}</span><strong>${item.needs_review ? "To confirm" : money(item.total_paise)}</strong></div>`).join("");
  const content = `<section class="surface ticket-card"><p class="eyebrow">Your print ticket</p><h1 class="ticket-number">${esc(order.ticket)}</h1><span class="ticket-status">${esc(pretty(order.status))}</span><p>${esc(message)}</p>${renderNotice(note)}
    <div class="ticket-divider"></div><div class="summary-line"><span>Payment</span><strong>${esc(pretty(order.payment_status))}${order.payment_method === "counter" ? " · at counter" : ""}</strong></div>
    ${summaryItems}${order.status === "payment_verification" ? `<div class="notice">No payment has been collected. The shop will confirm the page count and final price first.</div>` : `<div class="summary-total"><span>Order total</span><span>${money(order.total_paise)}</span></div>`}<p class="micro">Keep this ticket and show it at ${esc(state.shop.name)}.</p>
    <div class="ticket-actions"><button class="btn" type="button" data-share-ticket>Share ticket</button><button class="btn secondary" type="button" data-print-ticket>Save / print ticket</button><a class="btn secondary" href="${esc(trackingUrl)}">Open tracking link</a></div>
  </section>`;
  renderCustomerShell(content, "ticket");
  document.querySelector("[data-share-ticket]")?.addEventListener("click", async () => {
    try {
      if (navigator.share) await navigator.share({ title: `Print ticket ${order.ticket}`, text: `${state.shop.name} · ${order.ticket}`, url: trackingUrl });
      else { await navigator.clipboard.writeText(trackingUrl); toast("Tracking link copied.", "success"); }
    } catch (error) { if (error.name !== "AbortError") toast("Could not share the tracking link.", "error"); }
  });
  document.querySelector("[data-print-ticket]")?.addEventListener("click", () => window.print());
}

async function renderTrackingPage() {
  const params = new URLSearchParams(location.search);
  const slug = params.get("shop") || "";
  const ticket = params.get("ticket") || "";
  const token = decodeURIComponent(location.hash.slice(1));
  if (!slug || !ticket || !token) {
    root.innerHTML = `<main class="customer-page"><div class="customer-wrap"><div class="customer-topbar"><div class="wordmark">${brandMark("S")}<span>Track an order</span></div></div><section class="surface ticket-card"><h1>Open your secure tracking link</h1><p>The link includes a private verification token. Open the link from the order ticket to see progress.</p><div class="field"><label for="ticket-link">Tracking link</label><input id="ticket-link" type="url" placeholder="Paste the complete link from your ticket"></div><button class="btn" type="button" data-open-track>Open tracking</button></section></div></main>`;
    document.querySelector("[data-open-track]")?.addEventListener("click", () => {
      try { const target = new URL(document.getElementById("ticket-link").value); if (target.origin !== location.origin) throw new Error(); location.href = target.href; }
      catch { toast("Paste a valid tracking link for this shop.", "error"); }
    });
    return;
  }
  try {
    const [track, shop] = await Promise.all([
      api(`/api/public/shops/${encodeURIComponent(slug)}/orders/${encodeURIComponent(ticket)}`, { headers: { "X-Tracking-Token": token } }),
      api(`/api/public/shops/${encodeURIComponent(slug)}`),
    ]);
    state.shop = shop.shop;
    state.shopSlug = slug;
    state.trackingToken = token;
    const order = track.order;
    const payButton = order.status === "awaiting_payment" && order.payment_method === "razorpay" && order.payment_status !== "paid"
      ? `<button class="btn accent" data-track-pay>Pay online</button>` : "";
    const price = order.status === "payment_verification" ? `<div class="notice">The shop has not collected payment. The final total will appear here after page-count review.</div>`
      : `<div class="summary-total"><span>Order total</span><span>${money(order.total_paise)}</span></div>`;
    const content = `<section class="surface ticket-card"><p class="eyebrow">Order tracking</p><h1 class="ticket-number">${esc(order.ticket)}</h1><span class="ticket-status">${esc(pretty(order.status))}</span><p>${esc(STATUS_COPY[order.status] || "The shop is updating your order.")}</p>${order.customer_notice ? renderNotice(order.customer_notice) : ""}
      <div class="timeline">${["payment_verification", "awaiting_payment", "confirmed", "queued", "printing", "ready_for_collection", "completed"].map((status) => `<div class="timeline-row ${status === order.status ? "current" : ""}"><span class="timeline-dot"></span><span>${esc(pretty(status))}</span>${status === order.status ? `<small class="micro">Current</small>` : ""}</div>`).join("")}</div>
      <div class="ticket-divider"></div><div class="summary-line"><span>Payment</span><strong>${esc(pretty(order.payment_status))}${order.payment_method === "counter" ? " · at counter" : ""}</strong></div>${price}<div class="ticket-actions">${payButton}<a class="btn secondary" href="${esc(location.href)}">Copyable tracking link</a></div><p class="micro">Last updated ${esc(dateTime(order.updated_at))}. This page refreshes automatically.</p></section>`;
    renderCustomerShell(content, "ticket");
    document.querySelector("[data-track-pay]")?.addEventListener("click", async () => {
      state.placedOrder = { ...order, items: [] };
      await launchRazorpay(state.placedOrder);
    });
  } catch (error) {
    root.innerHTML = `<main class="customer-page"><div class="customer-wrap"><div class="customer-topbar"><div class="wordmark">${brandMark("S")}<span>Order tracking</span></div></div><section class="surface ticket-card">${renderNotice("This ticket could not be verified. Open the private tracking link that came with your order.", "error")}<p>${esc(error.message)}</p></section></div></main>`;
  }
}

const OWNER_NAV = [
  ["overview", "Overview", "◷"], ["orders", "Orders", "▤"], ["queue", "Print queue", "▧"],
  ["payments", "Payments & reports", "₹"], ["settings", "Shop settings", "⚙"],
  ["agent", "Print Agent", "⌁"], ["staff", "Staff access", "♙"],
];
const ORDER_STATUSES = ["payment_verification", "awaiting_payment", "confirmed", "queued", "preparing_documents", "ready_for_printing",
  "printing", "partially_completed", "ready_for_collection", "completed", "cancelled", "failed"];
const STAFF_PERMISSIONS = ["orders.view", "orders.download", "orders.manage", "printing.approve",
  "payments.manage", "reports.view", "prices.manage", "settings.manage", "staff.manage"];

function statusBadge(status) {
  return `<span class="status-badge ${esc(status)}">${esc(pretty(status))}</span>`;
}

function ownerNotice(text, kind = "") {
  return text ? `<div class="notice ${kind}" role="status">${esc(text)}</div>` : "";
}

function renderOwnerLogin(message = "") {
  state.owner = null;
  root.innerHTML = `<main class="customer-page"><section class="surface admin-login">
    <div class="wordmark">${brandMark("S")}<span>Smart Xerox <small class="micro">Shop workspace</small></span></div>
    <h1>Welcome back</h1><p>Sign in to manage shop orders, payments, and printing.</p>${ownerNotice(message, "error")}
    <form data-owner-login><div class="field"><label for="login-email">Email address</label><input id="login-email" name="email" type="email" autocomplete="username" required></div>
    <div class="field"><label for="login-password">Password</label><input id="login-password" name="password" type="password" autocomplete="current-password" required></div>
    <button class="btn full" type="submit">Sign in</button></form>
    <p class="micro">Customer orders are placed from your shop QR code.</p></section></main>`;
  document.querySelector("[data-owner-login]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const form = new FormData(event.currentTarget);
      const result = await api("/api/auth/login", { method: "POST", body: Object.fromEntries(form) });
      state.csrf = result.csrf_token;
      await startOwner();
    } catch (error) {
      button.disabled = false;
      renderOwnerLogin(error.message);
    }
  });
}

async function startOwner() {
  try {
    const result = await api("/api/auth/me");
    state.csrf = result.csrf_token;
    const user = result.user;
    let shops = result.shops || [];
    if (user.global_role === "super_admin") {
      const platform = await api("/api/super-admin/shops");
      shops = platform.shops || [];
    }
    const previousOwner = state.owner;
    let selectedShop;
    if (previousOwner?.user?.id === user.id && previousOwner.shop) {
      const slug = previousOwner.shop.slug;
      selectedShop = shops.find((shop) => shop.slug === slug) || previousOwner.shop;
    } else {
      const preferred = new URLSearchParams(location.search).get("shop");
      selectedShop = shops.find((shop) => shop.slug === preferred) || shops[0] || null;
    }
    state.owner = { ...(previousOwner || {}), user, shops, shop: selectedShop || shops[0] || null };
    if (user.global_role === "super_admin" && !state.owner.shop) state.ownerView = "platform";
    if (state.owner.shop && state.ownerView === "platform") state.ownerView = "overview";
    renderOwnerShell();
    await loadOwnerView();
    await refreshNotifications();
  } catch (error) {
    if (error.message.includes("Sign in")) renderOwnerLogin();
    else renderOwnerLogin(error.message);
  }
}

function renderOwnerShell() {
  const owner = state.owner;
  if (!owner) return renderOwnerLogin();
  const isPlatformAdmin = owner.user.global_role === "super_admin";
  const platformNav = isPlatformAdmin ? [["platform", "Platform", "◈"]] : [];
  const nav = [...platformNav, ...(owner.shop ? OWNER_NAV : [])];
  const shopSelect = isPlatformAdmin && owner.shops.length ? `<label class="sidebar-shop-select"><span>Manage a shop</span><select data-shop-select><option value="">Platform view</option>${owner.shops.map((shop) => `<option value="${esc(shop.slug)}" ${owner.shop?.slug === shop.slug ? "selected" : ""}>${esc(shop.name)}${shop.status === "suspended" ? " · suspended" : ""}</option>`).join("")}</select></label>` :
    `<div class="sidebar-shop"><strong>${esc(owner.shop?.name || "No shop yet")}</strong><span>${esc(owner.shop ? `/${owner.shop.slug}` : "Create your first shop")}</span></div>`;
  const navButtons = nav.map(([id, label, icon]) => `<button type="button" data-owner-view="${id}" class="${state.ownerView === id ? "active" : ""}"><span class="nav-icon" aria-hidden="true">${icon}</span>${label}</button>`).join("");
  const mobileNav = nav.filter((item) => item[0] !== "staff").slice(0, 5).map(([id, label, icon]) => `<button type="button" data-owner-view="${id}" class="${state.ownerView === id ? "active" : ""}"><span class="nav-icon" aria-hidden="true">${icon}</span><span>${esc(label.split(" ")[0])}</span></button>`).join("");
  const titles = {
    overview: ["Shop overview", "A live view of today's shop activity."],
    orders: ["Orders", "Search and manage customer print orders."],
    queue: ["Print queue", "Review jobs waiting for a printer or staff action."],
    payments: ["Payments & reports", "Review receipts, pending amounts, and refunds."],
    settings: ["Shop settings", "Manage your public profile, prices, and print rules."],
    agent: ["Local Print Agent", "Connect this shop computer to the secure print queue."],
    staff: ["Staff & activity", "Manage team access and review recent changes in this shop."],
    platform: ["Platform", "Manage registered shops and platform activity."],
  };
  const [title, subtitle] = titles[state.ownerView] || titles.overview;
  const initials = (owner.user.name || owner.user.email || "S").slice(0, 1).toUpperCase();
  root.innerHTML = `<div class="admin-app"><aside class="admin-sidebar"><div class="wordmark">${brandMark("S")}<span>Smart Xerox</span></div>${shopSelect}
      <nav class="side-nav" aria-label="Shop navigation">${navButtons}</nav>
      <div class="sidebar-footer"><a class="sidebar-link" href="/?shop=${encodeURIComponent(owner.shop?.slug || "")}">↗ Customer order page</a><button type="button" data-owner-logout>Sign out</button></div></aside>
    <main class="admin-main"><header class="admin-topbar"><div class="admin-heading"><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div><div class="top-actions">
      <button class="btn secondary small" type="button" data-notifications>Notifications <span data-unread-count></span></button>
      ${owner.shop ? `<span class="user-chip"><span class="avatar">${esc(initials)}</span><span>${esc(owner.user.name || owner.user.email)}</span></span>` : `<span class="user-chip"><span class="avatar">${esc(initials)}</span><span>${esc(owner.user.name || owner.user.email)}</span></span>`}</div></header>
      <div class="admin-content" id="owner-content"><div class="loading-block">Loading shop data…</div></div></main>
    <nav class="admin-mobilebar" aria-label="Mobile navigation">${mobileNav}</nav></div>`;
  document.querySelectorAll("[data-owner-view]").forEach((button) => button.addEventListener("click", () => {
    state.ownerView = button.dataset.ownerView;
    state.selectedOrder = null;
    renderOwnerShell();
    void loadOwnerView();
  }));
  document.querySelector("[data-shop-select]")?.addEventListener("change", (event) => {
    state.owner.shop = state.owner.shops.find((shop) => shop.slug === event.target.value) || null;
    state.ownerView = state.owner.shop ? "overview" : "platform";
    state.selectedOrder = null;
    renderOwnerShell();
    void loadOwnerView();
  });
  document.querySelector("[data-owner-logout]")?.addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST", body: {} }); } catch {}
    state.owner = null; state.csrf = ""; state.selectedOrder = null;
    renderOwnerLogin("You have signed out.");
  });
  document.querySelector("[data-notifications]")?.addEventListener("click", () => void showNotifications());
}

function ownerContent(html) {
  const content = document.getElementById("owner-content");
  if (content) content.innerHTML = html;
}

async function loadOwnerView() {
  if (!state.owner) return;
  if (state.ownerView === "platform") return loadPlatformView();
  if (!state.owner.shop) {
    ownerContent(`<section class="surface empty-state"><span class="empty-mark">＋</span><h3>No shop is connected</h3><p>Create a shop from the platform view to start receiving print orders.</p><button class="btn" data-platform-open>Open platform</button></section>`);
    document.querySelector("[data-platform-open]")?.addEventListener("click", () => { state.ownerView = "platform"; renderOwnerShell(); void loadOwnerView(); });
    return;
  }
  try {
    if (state.ownerView === "overview") return await loadOverview();
    if (state.ownerView === "orders") return await loadOrders();
    if (state.ownerView === "queue") return await loadQueue();
    if (state.ownerView === "payments") return await loadPayments();
    if (state.ownerView === "settings") return await loadSettings();
    if (state.ownerView === "agent") return await loadAgents();
    if (state.ownerView === "staff") return await loadStaff();
  } catch (error) {
    ownerContent(`<section class="surface admin-panel">${ownerNotice(error.message, "error")}<button class="btn secondary" data-reload-view>Retry</button></section>`);
    document.querySelector("[data-reload-view]")?.addEventListener("click", () => void loadOwnerView());
  }
}

async function loadOverview() {
  const [dashboard, orders, agentResult] = await Promise.all([
    api("/api/admin/dashboard"), api("/api/admin/orders?limit=6&offset=0"), api("/api/admin/agents").catch(() => ({ agents: [] })),
  ]);
  state.agents = agentResult.agents || [];
  const d = dashboard.dashboard;
  const cards = [
    ["Today's orders", d.today_orders, "New tickets today", "◷"],
    ["Total orders", d.total_orders, "All tickets in this shop", "▤"],
    ["Waiting", d.pending_orders, "Orders not yet completed", "▤"],
    ["Printing", d.printing_orders, "Currently being handled", "▧"],
    ["Completed", d.completed_orders, `${d.failed_orders} failed or needs review`, "✓"],
    ["Today's revenue", money(d.today_revenue_paise), "Verified payments received", "₹"],
    ["Payments pending", money(d.pending_amount_paise), "Orders not yet paid", "◌"],
    ["Pages printed", d.printed.pages, `${d.printed.bw_pages} B&W · ${d.printed.color_pages} color`, "▤"],
    ["Print Agents", d.print_agents_online ? "Online" : "Offline", `${d.print_agents_online} connected device${d.print_agents_online === 1 ? "" : "s"}`, "⌁"],
  ];
  const cardHtml = cards.map(([label, value, foot, icon]) => `<article class="surface stat-card"><div class="stat-top"><span>${esc(label)}</span><span class="stat-icon">${icon}</span></div><div class="stat-value">${esc(value)}</div><div class="stat-foot">${esc(foot)}</div></article>`).join("");
  const recent = orders.orders.length ? `<div class="table-wrap"><table><thead><tr><th>Ticket</th><th>Customer</th><th>Total</th><th>Payment</th><th>Status</th></tr></thead><tbody>${orders.orders.map((order) => `<tr data-open-order="${esc(order.id)}"><td><span class="table-primary">${esc(order.ticket)}</span><div class="table-secondary">${esc(dateTime(order.created_at))}</div></td><td>${esc(order.customer_name || "Walk-in customer")}<div class="table-secondary">${esc(order.customer_phone || "No phone provided")}</div></td><td>${money(order.total_paise)}</td><td>${statusBadge(order.payment_status)}</td><td>${statusBadge(order.status)}</td></tr>`).join("")}</tbody></table></div>` : emptyState("No orders yet", "New QR orders will appear here.");
  ownerContent(`<div class="stats-grid">${cardHtml}</div><section class="surface admin-panel"><div class="admin-panel-head"><div><h2>Recent orders</h2><p>Latest customer tickets across this shop.</p></div><button class="btn secondary small" data-go-orders>All orders</button></div>${recent}</section>
    ${state.agents.length ? `<section class="surface admin-panel detail-section spaced"><div class="admin-panel-head"><div><h2>Printer connections</h2><p>Agent heartbeat and local printer status.</p></div><button class="btn secondary small" data-go-agent>Manage agents</button></div>${state.agents.map(renderAgentCard).join("")}</section>` : `<section class="surface admin-panel detail-section spaced"><div class="admin-panel-head"><div><h2>Automatic printing</h2><p>Pair the shop computer to receive approved print jobs.</p></div><button class="btn secondary small" data-go-agent>Set up Print Agent</button></div><p class="micro">Manual printing is available now. Automatic printing requires an agent paired to a local printer.</p></section>`}`);
  document.querySelectorAll("[data-open-order]").forEach((row) => row.addEventListener("click", () => openOrder(row.dataset.openOrder)));
  document.querySelector("[data-go-orders]")?.addEventListener("click", () => setOwnerView("orders"));
  document.querySelector("[data-go-agent]")?.addEventListener("click", () => setOwnerView("agent"));
}

function emptyState(title, message) {
  return `<div class="empty-state"><span class="empty-mark">⌁</span><h3>${esc(title)}</h3><p>${esc(message)}</p></div>`;
}

function setOwnerView(view) {
  state.ownerView = view;
  state.selectedOrder = null;
  renderOwnerShell();
  void loadOwnerView();
}

async function loadOrders() {
  if (state.selectedOrder) return loadOrderDetail(state.selectedOrder);
  const filters = state.ownerFilters;
  const params = new URLSearchParams({ limit: "25", offset: String(filters.offset || 0) });
  for (const key of ["search", "status", "payment_status", "from", "to"]) if (filters[key]) params.set(key, filters[key]);
  const result = await api(`/api/admin/orders?${params}`);
  const rows = result.orders.length ? `<div class="table-wrap"><table><thead><tr><th>Ticket / created</th><th>Customer</th><th>Documents</th><th>Pages</th><th>Total</th><th>Payment</th><th>Order</th></tr></thead><tbody>${result.orders.map((order) => `<tr data-open-order="${esc(order.id)}"><td><span class="table-primary">${esc(order.ticket)}</span><div class="table-secondary">${esc(dateTime(order.created_at))}</div></td><td>${esc(order.customer_name || "Walk-in")}<div class="table-secondary">${esc(order.customer_phone || "—")}</div></td><td>${order.document_count}</td><td>${order.pages}</td><td>${money(order.total_paise)}</td><td>${statusBadge(order.payment_status)}</td><td>${statusBadge(order.status)}</td></tr>`).join("")}</tbody></table></div>` : emptyState("No matching orders", "Try clearing a filter or wait for a new customer order.");
  ownerContent(`<section class="surface admin-panel"><div class="toolbar"><label class="field search"><span class="sr-only">Search orders</span><input data-filter="search" type="search" value="${esc(filters.search)}" placeholder="Ticket, customer, phone, or file"></label>
    <label class="field"><span class="sr-only">Order status</span><select data-filter="status"><option value="">All order statuses</option>${ORDER_STATUSES.map((status) => `<option value="${status}" ${filters.status === status ? "selected" : ""}>${esc(pretty(status))}</option>`).join("")}</select></label>
    <label class="field"><span class="sr-only">Payment status</span><select data-filter="payment_status"><option value="">All payment statuses</option>${["pending", "paid", "failed", "refunded"].map((status) => `<option value="${status}" ${filters.payment_status === status ? "selected" : ""}>${esc(pretty(status))}</option>`).join("")}</select></label>
    <label class="field"><span class="sr-only">From</span><input data-filter="from" type="date" value="${esc(filters.from)}" aria-label="From date"></label><label class="field"><span class="sr-only">To</span><input data-filter="to" type="date" value="${esc(filters.to)}" aria-label="To date"></label>
    <button class="btn secondary small" data-clear-filters>Clear</button></div><p class="micro">${result.total} order${result.total === 1 ? "" : "s"} found · newest first</p>${rows}
    <div class="pagination"><span>Showing ${result.orders.length ? result.offset + 1 : 0}–${result.offset + result.orders.length} of ${result.total}</span><div class="detail-actions"><button class="btn secondary small" data-page="prev" ${result.offset <= 0 ? "disabled" : ""}>Previous</button><button class="btn secondary small" data-page="next" ${result.offset + result.orders.length >= result.total ? "disabled" : ""}>Next</button></div></div></section>`);
  document.querySelectorAll("[data-open-order]").forEach((row) => row.addEventListener("click", () => openOrder(row.dataset.openOrder)));
  document.querySelectorAll("[data-filter]").forEach((input) => input.addEventListener(input.type === "search" ? "input" : "change", () => {
    state.ownerFilters[input.dataset.filter] = input.value;
    state.ownerFilters.offset = 0;
    if (input.type === "search") {
      clearTimeout(state.orderSearchTimer);
      state.orderSearchTimer = setTimeout(() => void loadOrders(), 250);
    } else void loadOrders();
  }));
  document.querySelector("[data-clear-filters]")?.addEventListener("click", () => { state.ownerFilters = { search: "", status: "", payment_status: "", from: "", to: "", offset: 0 }; void loadOrders(); });
  document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => { state.ownerFilters.offset += button.dataset.page === "next" ? 25 : -25; void loadOrders(); }));
}

function openOrder(orderId) {
  state.selectedOrder = orderId;
  void loadOrderDetail(orderId);
}

async function loadOrderDetail(orderId) {
  const result = await api(`/api/admin/orders/${encodeURIComponent(orderId)}`);
  const order = result.order;
  const docs = order.documents.map((doc) => {
    const settings = doc.settings || {};
    const opts = `${pretty(settings.color)} · ${pretty(settings.sides)} · ${esc(settings.paper_size)} · ${settings.copies} copy${settings.copies === 1 ? "" : "ies"} · ${esc(settings.page_selection || "all pages")}`;
    return `<div class="doc-row"><span class="file-type">${esc(doc.mime_type.split("/").pop().toUpperCase())}</span><div class="doc-row-main"><strong title="${esc(doc.original_name)}">${esc(doc.original_name)}</strong><small>${doc.page_count ?? "?"} pages · ${sizeLabel(doc.size_bytes)} · ${opts}</small></div>
      <button class="btn secondary small" data-doc-preview="${esc(doc.id)}">Preview</button><button class="btn secondary small" data-doc-download="${esc(doc.id)}">Download</button></div>`;
  }).join("");
  const pageReview = order.status === "payment_verification" ? `<section class="surface detail-section"><h2>Verify pages and confirm the price</h2><p class="micro">Enter the page count you checked. The server will recalculate the order total and notify the customer before payment.</p>
    <form class="settings-form" data-page-review>${order.documents.filter((doc) => doc.needs_review).map((doc) => `<label class="field"><span>${esc(doc.original_name)} · verify page count</span><input data-review-pages="${esc(doc.id)}" type="number" min="1" max="${state.settings?.general?.max_pages || 5000}" required></label>`).join("")}
    <label class="field"><span>Customer payment method</span><select name="payment_method" required>${order.available_payment_methods?.counter ? `<option value="counter">Pay at the counter</option>` : ""}${order.available_payment_methods?.razorpay ? `<option value="razorpay">Pay online using Razorpay</option>` : ""}</select></label>
    <button class="btn" type="submit" ${!order.available_payment_methods?.counter && !order.available_payment_methods?.razorpay ? "disabled" : ""}>Confirm count and final price</button></form>
    ${!order.available_payment_methods?.counter && !order.available_payment_methods?.razorpay ? ownerNotice("Enable counter payments or configure Razorpay before confirming a price.", "error") : ""}</section>` : "";
  const jobs = order.print_jobs.length ? order.print_jobs.map((job) => `<div class="job-row"><span class="file-type">${job.method === "manual" ? "MAN" : "AUTO"}</span><div class="job-row-main"><strong>${esc(job.original_name)}</strong><small>${esc(job.method)} · ${esc(job.error_message || job.status)} · attempt ${job.attempts + 1}</small></div>${statusBadge(job.status)}
    ${job.status === "queued" && job.method === "manual" ? `<button class="btn secondary small" data-manual-start="${job.id}">Mark printing</button>` : ""}
    ${["printing", "queued"].includes(job.status) && job.method === "manual" ? `<button class="btn secondary small" data-manual-done="${job.id}">Mark printed</button>` : ""}
    ${job.status === "failed" ? `<button class="btn secondary small" data-retry-method="${job.method}" data-retry-job="${job.id}">Retry ${job.method}</button>` : ""}
    ${job.method === "automatic" && job.status === "submitting" ? `<button class="btn secondary small" data-resolve-job="${job.id}" data-resolve-result="printed">Confirm printed</button><button class="btn secondary small" data-resolve-job="${job.id}" data-resolve-result="not_printed">Confirm not printed</button>` : ""}
    ${job.method === "automatic" && ["submitted", "spooler_reported"].includes(job.status) ? `<button class="btn secondary small" data-confirm-job="${job.id}">Confirm physical print</button>` : ""}</div>`).join("") : emptyState("No print jobs", "Create manual jobs now or approve this order for the paired Print Agent.");
  const statuses = ORDER_STATUSES.map((status) => `<option value="${status}" ${order.status === status ? "selected" : ""}>${esc(pretty(status))}</option>`).join("");
  const jobsStarted = order.print_jobs.some((job) => ["claimed", "downloading", "submitting", "submitted", "spooler_reported", "printing", "completed"].includes(job.status));
  const canCancel = ["payment_verification", "awaiting_payment", "confirmed", "queued", "preparing_documents", "ready_for_printing"].includes(order.status) && !jobsStarted;
  ownerContent(`<div class="detail-actions"><button class="btn subtle small" data-back-orders>← All orders</button><button class="btn secondary small" data-refresh-order>Refresh order</button></div>
    <div class="order-detail"><div><section class="surface detail-section"><div class="admin-panel-head"><div><p class="eyebrow">Order ticket</p><h2>${esc(order.ticket)}</h2></div>${statusBadge(order.status)}</div>
      <dl class="detail-kv"><dt>Customer</dt><dd>${esc(order.customer_name || "Walk-in customer")}</dd><dt>Phone</dt><dd>${esc(order.customer_phone || "Not provided")}</dd><dt>Created</dt><dd>${esc(dateTime(order.created_at))}</dd><dt>Payment</dt><dd>${statusBadge(order.payment_status)} · ${esc(pretty(order.payment_method))}</dd><dt>Order total</dt><dd>${money(order.total_paise)}</dd><dt>Subtotal / tax</dt><dd>${money(order.subtotal_paise)} / ${money(order.tax_paise)}</dd></dl>
      <div class="detail-actions">${order.payment_method === "counter" && order.payment_status !== "paid" ? `<button class="btn accent" data-counter-paid>Record counter payment received</button>` : ""}
        ${order.payment_status === "paid" && !order.print_jobs.some((job) => !["completed", "failed", "cancelled"].includes(job.status)) ? `<button class="btn" data-create-print="manual" data-confirm-reprint="${order.print_jobs.some((job) => job.method === "manual")}">Create manual print jobs</button>${state.settings?.printing?.automatic_enabled !== false ? `<button class="btn secondary" data-create-print="automatic" data-confirm-reprint="${order.print_jobs.some((job) => job.method === "automatic")}">Approve automatic jobs</button>` : ""}` : ""}
        ${canCancel ? `<button class="btn danger small" data-cancel-order>Cancel order</button>` : ""}</div>
      <div class="detail-section"><h2>Uploaded documents</h2>${docs || emptyState("Documents unavailable", "No documents are attached to this order.")}</div>
      </section>${pageReview}<section class="surface detail-section"><h2>Print jobs</h2>${jobs}</section>
      <section class="surface detail-section"><h2>Order status</h2><form class="action-row" data-order-status><label class="field"><span class="sr-only">Status</span><select name="status">${statuses}</select></label><button class="btn secondary" type="submit">Update status</button></form><p class="micro">The server checks allowed status changes and requires verified payment before production.</p></section></div>
      <aside><section class="surface detail-section"><h2>Payment history</h2>${order.payments.length ? order.payments.map((payment) => `<div class="job-row"><div class="job-row-main"><strong>${esc(pretty(payment.provider))} · ${money(payment.amount_paise)}</strong><small>${esc(payment.provider_payment_id || payment.provider_order_id || "Counter record")} · ${esc(dateTime(payment.created_at))}</small></div>${statusBadge(payment.status)}</div>`).join("") : `<p class="micro">No payment event is recorded yet.</p>`}
      ${order.payment_status === "paid" ? `<button class="btn secondary small" data-refund-order>Issue or record refund</button>` : ""}</section>
      <section class="surface detail-section"><h2>Pricing at order time</h2><pre class="code-box">${esc(JSON.stringify(order.pricing_snapshot, null, 2))}</pre></section></aside></div>`);
  document.querySelector("[data-back-orders]")?.addEventListener("click", () => { state.selectedOrder = null; void loadOrders(); });
  document.querySelector("[data-refresh-order]")?.addEventListener("click", () => void loadOrderDetail(orderId));
  document.querySelector("[data-counter-paid]")?.addEventListener("click", () => void mutateOrder(`/api/admin/orders/${orderId}/counter-payment`, "POST", {}, "Counter payment recorded."));
  document.querySelectorAll("[data-doc-preview]").forEach((button) => button.addEventListener("click", () => void openAdminDocument(orderId, button.dataset.docPreview, true)));
  document.querySelectorAll("[data-doc-download]").forEach((button) => button.addEventListener("click", () => void openAdminDocument(orderId, button.dataset.docDownload, false)));
  document.querySelectorAll("[data-create-print]").forEach((button) => button.addEventListener("click", () => void createPrintJobs(orderId, button.dataset.createPrint, button.dataset.confirmReprint === "true")));
  document.querySelectorAll("[data-manual-start]").forEach((button) => button.addEventListener("click", () => void mutateOrder(`/api/admin/print-jobs/${button.dataset.manualStart}/state`, "POST", { status: "printing" }, "Print marked as started.")));
  document.querySelectorAll("[data-manual-done]").forEach((button) => button.addEventListener("click", () => {
    if (confirm("Confirm that this document physically came out of the printer?")) void mutateOrder(`/api/admin/print-jobs/${button.dataset.manualDone}/confirm`, "POST", {}, "Physical print confirmed.");
  }));
  document.querySelectorAll("[data-confirm-job]").forEach((button) => button.addEventListener("click", () => {
    if (confirm("Confirm that you have checked the printer and this document physically printed?")) void mutateOrder(`/api/admin/print-jobs/${button.dataset.confirmJob}/confirm`, "POST", {}, "Physical print confirmed.");
  }));
  document.querySelectorAll("[data-resolve-job]").forEach((button) => button.addEventListener("click", () => void resolvePrintJob(button.dataset.resolveJob, button.dataset.resolveResult)));
  document.querySelectorAll("[data-retry-method]").forEach((button) => button.addEventListener("click", () => {
    if (confirm(`Create a new ${button.dataset.retryMethod} attempt? Check first that the earlier attempt did not print.`)) {
      void createPrintJobs(orderId, button.dataset.retryMethod, true);
    }
  }));
  document.querySelector("[data-cancel-order]")?.addEventListener("click", () => {
    const text = order.payment_status === "paid"
      ? "Cancel this order? Its payment will remain collected; record a refund separately. Queued, unstarted jobs will be stopped."
      : "Cancel this order? The ticket will remain available for the customer.";
    if (confirm(text)) {
      void mutateOrder(`/api/admin/orders/${orderId}`, "PATCH", { status: "cancelled" }, "Order cancelled.");
    }
  });
  document.querySelector("[data-order-status]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    void mutateOrder(`/api/admin/orders/${orderId}`, "PATCH", { status: new FormData(event.currentTarget).get("status") }, "Order status updated.");
  });
  document.querySelector("[data-refund-order]")?.addEventListener("click", () => void startRefund(order));
  document.querySelector("[data-page-review]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget, values = Object.fromEntries([...form.querySelectorAll("[data-review-pages]")].map((input) => [input.dataset.reviewPages, Number(input.value)]));
    const paymentMethod = new FormData(form).get("payment_method");
    try {
      await api(`/api/admin/orders/${orderId}/review`, { method: "POST", body: { page_counts: values, payment_method: paymentMethod } });
      toast("Page count and final price confirmed. The customer has been notified.", "success");
      await loadOrderDetail(orderId);
    } catch (error) { toast(error.message, "error"); }
  });
}

async function mutateOrder(path, method, body, message) {
  try { await api(path, { method, body }); toast(message, "success"); await loadOrderDetail(state.selectedOrder); }
  catch (error) { toast(error.message, "error"); }
}

async function createPrintJobs(orderId, method, reprintConfirmed = false) {
  const text = `${reprintConfirmed ? "This order has earlier print attempts. Check that they did not produce an extra copy. " : ""}${method === "automatic"
    ? "Approve the Print Agent to send these paid documents to the selected printer. Review any color or large-job warning and confirm the correct printer and print settings."
    : "Create manual print jobs. You will still need to open each document and confirm when it physically prints."}`;
  if (!confirm(text)) return;
  try {
    const result = await api(`/api/admin/orders/${orderId}/print-jobs`, { method: "POST", body: { method, confirm_risk: method === "automatic", confirm_reprint: reprintConfirmed } });
    toast(`${result.jobs.length} print job${result.jobs.length === 1 ? "" : "s"} queued.`, "success");
    await loadOrderDetail(orderId);
  } catch (error) { toast(error.message, "error"); }
}

async function resolvePrintJob(jobId, result) {
  const message = result === "printed"
    ? "Confirm the printer status and tray; this will mark the job physically printed."
    : "Confirm that the spooler has no matching job. This will allow a safe retry.";
  if (!confirm(message)) return;
  try { await api(`/api/admin/print-jobs/${jobId}/resolve`, { method: "POST", body: { result } }); await loadOrderDetail(state.selectedOrder); }
  catch (error) { toast(error.message, "error"); }
}

async function openAdminDocument(orderId, documentId, preview) {
  try {
    const response = await fetch(`/api/admin/orders/${encodeURIComponent(orderId)}/documents/${encodeURIComponent(documentId)}/download`, {
      credentials: "same-origin", headers: { "X-Shop-Slug": state.owner.shop.slug }, cache: "no-store",
    });
    if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error?.error?.message || "Document could not be opened."); }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    if (!preview) {
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = "print-document"; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      return;
    }
    showModal("Document preview", `<iframe class="document-preview" title="Private document preview" src="${url}"></iframe><div class="detail-actions"><button class="btn secondary" data-modal-print>Open print dialog</button><button class="btn" data-modal-close>Done</button></div>`);
    document.querySelector("[data-modal-print]")?.addEventListener("click", () => {
      const frame = document.querySelector(".document-preview");
      try { frame.contentWindow.focus(); frame.contentWindow.print(); } catch { toast("Use the browser preview's print command.", "error"); }
    });
    document.querySelector("[data-modal-close]")?.addEventListener("click", () => { closeModal(); URL.revokeObjectURL(url); });
  } catch (error) { toast(error.message, "error"); }
}

async function loadQueue() {
  const result = await api("/api/admin/print-jobs");
  const jobs = result.jobs || [];
  const body = jobs.length ? jobs.map((job, index) => `<tr><td>${index + 1}</td><td><button class="table-link" data-open-order="${esc(job.order_id)}">${esc(job.ticket)}</button><div class="table-secondary">${esc(dateTime(job.created_at))}</div></td>
    <td><span class="table-primary">${esc(job.original_name)}</span><div class="table-secondary">${job.page_count ?? "?"} pages · ${pretty(job.settings.color)} · ${pretty(job.settings.sides)} · ${esc(job.settings.paper_size)} · ${job.settings.copies} copies</div></td>
    <td>${statusBadge(job.payment_status)}</td><td>${statusBadge(job.status)}<div class="table-secondary">${esc(job.method)}${job.agent_id ? ` · agent ${esc(job.agent_id.slice(0, 8))}` : ""}</div></td>
    <td><div class="detail-actions">${job.status === "queued" ? `<button class="btn secondary small" data-queue-action="pause" data-job="${job.id}">Pause</button>` : ""}${job.status === "paused" ? `<button class="btn secondary small" data-queue-action="resume" data-job="${job.id}">Resume</button>` : ""}
      ${["queued", "paused"].includes(job.status) ? `<button class="btn subtle small" data-priority="-1" data-job="${job.id}" aria-label="Lower priority">↓</button><button class="btn subtle small" data-priority="1" data-job="${job.id}" aria-label="Raise priority">↑</button><button class="btn danger small" data-queue-action="cancel" data-job="${job.id}">Cancel job</button>` : ""}
      ${job.method === "automatic" && job.status === "failed" ? `<button class="btn secondary small" data-fallback="${job.order_id}">Move to manual</button>` : ""}
      ${job.method === "manual" && job.status === "queued" ? `<button class="btn secondary small" data-manual-start="${job.id}">Start manual</button>` : ""}
      ${job.method === "manual" && ["queued", "printing"].includes(job.status) ? `<button class="btn secondary small" data-manual-done="${job.id}">Confirm printed</button>` : ""}
      ${job.method === "automatic" && job.status === "submitting" ? `<button class="btn secondary small" data-resolve-queue="${job.id}" data-result="printed">Printed</button><button class="btn secondary small" data-resolve-queue="${job.id}" data-result="not_printed">Not printed</button>` : ""}
    </div></td></tr>`).join("") : "";
  ownerContent(`<section class="surface admin-panel"><div class="admin-panel-head"><div><h2>All print jobs</h2><p>${jobs.length} tracked job${jobs.length === 1 ? "" : "s"} · queued work runs only after payment is verified.</p></div><button class="btn secondary small" data-refresh-queue>Refresh</button></div>
    ${jobs.length ? `<div class="table-wrap"><table><thead><tr><th>Position</th><th>Ticket</th><th>Document / settings</th><th>Payment</th><th>Job state</th><th>Actions</th></tr></thead><tbody>${body}</tbody></table></div>` : emptyState("Queue is clear", "Approved manual and automatic jobs will appear here.")}</section>
    <section class="notice detail-section spaced"><span>Automatic submissions can stay in “Submitting” if the connection drops. Check the local spooler before resolving that job as printed or not printed.</span></section>`);
  document.querySelector("[data-refresh-queue]")?.addEventListener("click", () => void loadQueue());
  document.querySelectorAll("[data-open-order]").forEach((button) => button.addEventListener("click", () => { state.ownerView = "orders"; state.selectedOrder = button.dataset.openOrder; renderOwnerShell(); void loadOwnerView(); }));
  document.querySelectorAll("[data-queue-action]").forEach((button) => button.addEventListener("click", () => void queueMutation(button.dataset.job, { action: button.dataset.queueAction })));
  document.querySelectorAll("[data-priority]").forEach((button) => button.addEventListener("click", () => {
    const job = jobs.find((item) => item.id === button.dataset.job);
    void queueMutation(button.dataset.job, { action: "priority", priority: Math.max(-100, Math.min(100, (job?.priority || 0) + Number(button.dataset.priority))) });
  }));
  document.querySelectorAll("[data-fallback]").forEach((button) => button.addEventListener("click", () => {
    if (confirm("Create manual print jobs for this paid order? Check that the automatic spooler did not print it before trying again.")) void createPrintJobs(button.dataset.fallback, "manual", true);
  }));
  document.querySelectorAll("[data-manual-start]").forEach((button) => button.addEventListener("click", () => void queueState(button.dataset.manualStart, { status: "printing" })));
  document.querySelectorAll("[data-manual-done]").forEach((button) => button.addEventListener("click", () => {
    if (confirm("Confirm that the document physically printed?")) void api(`/api/admin/print-jobs/${button.dataset.manualDone}/confirm`, { method: "POST", body: {} }).then(() => loadQueue()).catch((error) => toast(error.message, "error"));
  }));
  document.querySelectorAll("[data-resolve-queue]").forEach((button) => button.addEventListener("click", () => {
    if (confirm(button.dataset.result === "printed" ? "Confirm the document physically printed?" : "Confirm the spooler has no matching job?")) {
      void api(`/api/admin/print-jobs/${button.dataset.resolveQueue}/resolve`, { method: "POST", body: { result: button.dataset.result } }).then(() => loadQueue()).catch((error) => toast(error.message, "error"));
    }
  }));
}

async function queueMutation(jobId, body) {
  try { await api(`/api/admin/print-jobs/${jobId}/queue`, { method: "POST", body }); await loadQueue(); }
  catch (error) { toast(error.message, "error"); }
}

async function queueState(jobId, body) {
  try { await api(`/api/admin/print-jobs/${jobId}/state`, { method: "POST", body }); await loadQueue(); }
  catch (error) { toast(error.message, "error"); }
}

async function loadPayments() {
  const result = await api("/api/admin/payments");
  const t = result.totals;
  const cards = [["Collected", money(t.revenue_paise)], ["Today", money(t.today_revenue_paise)], ["This week", money(t.week_revenue_paise)],
    ["This month", money(t.month_revenue_paise)], ["Online", money(t.online_paise)], ["At counter", money(t.counter_paise)],
    ["Pending", money(t.pending_paise)], ["Refunds", money(t.refunds_paise)], ["Failed", t.failed_count]];
  const paymentRows = result.payments.map((payment) => `<tr><td><button class="table-link" data-open-order="${esc(payment.order_id)}">${esc(payment.ticket)}</button><div class="table-secondary">${esc(payment.customer_name || "Customer")}</div></td>
    <td>${esc(pretty(payment.provider))}</td><td>${esc(payment.provider_payment_id || payment.provider_order_id || (payment.provider === "counter" ? "Counter collection" : "—"))}</td><td>${money(payment.amount_paise)}</td><td>${statusBadge(payment.status)}</td><td>${esc(dateTime(payment.created_at))}</td>
    <td>${payment.status === "captured" || payment.status === "paid" ? `<button class="btn secondary small" data-refund-payment="${payment.order_id}">Refund</button>` : ""}</td></tr>`).join("");
  const refundRows = result.refunds.map((refund) => `<tr><td><button class="table-link" data-open-order="${esc(refund.order_id)}">${esc(refund.ticket)}</button></td><td>${money(refund.amount_paise)}</td><td>${esc(refund.reason || "—")}</td><td>${esc(refund.provider_refund_id || "Counter refund")}</td><td>${statusBadge(refund.status)}</td><td>${esc(dateTime(refund.created_at))}</td></tr>`).join("");
  ownerContent(`<div class="stats-grid payment-stats">${cards.map(([label, value]) => `<article class="surface stat-card"><div class="stat-top">${esc(label)}</div><div class="stat-value">${esc(value)}</div></article>`).join("")}</div>
    <section class="surface admin-panel"><div class="admin-panel-head"><div><h2>Payment history</h2><p>Verified gateway captures and staff recorded counter payments.</p></div><div class="detail-actions"><button class="btn secondary small" data-download-report>Orders CSV</button><button class="btn secondary small" data-download-payment-report>Payments CSV</button></div></div>
    ${result.payments.length ? `<div class="table-wrap"><table><thead><tr><th>Order</th><th>Method</th><th>Transaction reference</th><th>Amount</th><th>State</th><th>Received</th><th></th></tr></thead><tbody>${paymentRows}</tbody></table></div>` : emptyState("No payments yet", "Verified online and counter collections will appear here.")}</section>
    <section class="surface admin-panel detail-section spaced"><div class="admin-panel-head"><div><h2>Refund records</h2><p>Counter refunds are recorded by staff; Razorpay refunds are sent securely from the backend.</p></div></div>${refundRows ? `<div class="table-wrap"><table><thead><tr><th>Order</th><th>Amount</th><th>Reason</th><th>Provider ref.</th><th>State</th><th>Created</th></tr></thead><tbody>${refundRows}</tbody></table></div>` : emptyState("No refunds", "Refunds and counter adjustments will be recorded here.")}</section>`);
  document.querySelectorAll("[data-open-order]").forEach((button) => button.addEventListener("click", () => { state.ownerView = "orders"; state.selectedOrder = button.dataset.openOrder; renderOwnerShell(); void loadOwnerView(); }));
  document.querySelectorAll("[data-refund-payment]").forEach((button) => button.addEventListener("click", async () => {
    try {
      const { order } = await api(`/api/admin/orders/${button.dataset.refundPayment}`);
      await startRefund(order);
    } catch (error) { toast(error.message, "error"); }
  }));
  document.querySelector("[data-download-report]")?.addEventListener("click", () => void downloadReport());
  document.querySelector("[data-download-payment-report]")?.addEventListener("click", () => void downloadPaymentReport());
}

async function downloadReport() {
  try {
    const response = await fetch("/api/admin/reports.csv", { credentials: "same-origin", headers: { "X-Shop-Slug": state.owner.shop.slug }, cache: "no-store" });
    if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error?.error?.message || "Report could not be downloaded."); }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = "shop-orders.csv"; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (error) { toast(error.message, "error"); }
}

async function downloadPaymentReport() {
  try {
    const response = await fetch("/api/admin/payments.csv", { credentials: "same-origin", headers: { "X-Shop-Slug": state.owner.shop.slug }, cache: "no-store" });
    if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error?.error?.message || "Payment report could not be downloaded."); }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = "shop-payments.csv"; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (error) { toast(error.message, "error"); }
}

function startRefund(order) {
  const paid = (order.payments || []).filter((payment) => ["captured", "paid"].includes(payment.status));
  const remaining = Math.max(0, paid.reduce((sum, payment) => sum + payment.amount_paise, 0) -
    (order.refunds || []).filter((refund) => !["failed", "rejected"].includes(refund.status)).reduce((sum, refund) => sum + refund.amount_paise, 0));
  if (!remaining) { toast("No refundable balance remains for this payment.", "error"); return; }
  showModal(`Refund ${order.ticket}`, `<p class="micro">Remaining recorded balance: ${money(remaining)}. A Razorpay refund is sent to the original payment method. A counter refund records the amount you have paid back.</p>
    <form class="settings-form" data-refund-form><label class="field"><span>Amount (₹)</span><input name="amount" type="number" min="0.01" max="${(remaining / 100).toFixed(2)}" step="0.01" value="${(remaining / 100).toFixed(2)}" required></label>
    <label class="field"><span>Reason</span><input name="reason" maxlength="240" placeholder="Duplicate print, cancelled order, adjustment"></label>
    <div class="detail-actions"><button class="btn secondary" type="button" data-modal-close>Keep payment</button><button class="btn danger" type="submit">Submit refund</button></div></form>`);
  document.querySelector("[data-modal-close]")?.addEventListener("click", closeModal);
  document.querySelector("[data-refund-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const amountPaise = Math.round(Number(values.get("amount")) * 100);
    if (!Number.isSafeInteger(amountPaise) || amountPaise <= 0 || amountPaise > remaining) { toast("Enter a refund amount within the remaining balance.", "error"); return; }
    const key = crypto.randomUUID().replaceAll("-", "").slice(0, 24);
    const submit = event.currentTarget.querySelector("[type=submit]"); submit.disabled = true;
    try {
      const response = await api(`/api/admin/orders/${order.id}/refunds`, { method: "POST", body: {
        amount_paise: amountPaise, reason: String(values.get("reason") || ""), idempotency_key: key,
      } });
      closeModal(); toast(`Refund ${pretty(response.refund.status).toLowerCase()}.`, "success");
      if (state.ownerView === "payments") await loadPayments(); else await loadOrderDetail(order.id);
    } catch (error) { submit.disabled = false; toast(error.message, "error"); }
  });
}

async function loadSettings() {
  const [response, agents] = await Promise.all([api("/api/admin/settings"), api("/api/admin/agents").catch(() => ({ agents: [] }))]);
  state.settings = response.settings;
  state.agents = agents.agents || [];
  const tabs = [["profile", "Shop profile"], ["pricing", "Pricing"], ["general", "Customer orders"], ["printing", "Printing"]];
  const tabButtons = tabs.map(([key, label]) => `<button type="button" data-settings-tab="${key}" class="${state.settingsTab === key ? "active" : ""}">${label}</button>`).join("");
  ownerContent(`<div class="settings-layout"><nav class="surface settings-tabs" aria-label="Settings sections">${tabButtons}</nav><div class="surface settings-section">${renderSettingsSection()}</div></div>`);
  document.querySelectorAll("[data-settings-tab]").forEach((button) => button.addEventListener("click", () => { state.settingsTab = button.dataset.settingsTab; void loadSettings(); }));
  bindSettingsForm();
}

function renderSettingsSection() {
  const s = state.settings;
  if (state.settingsTab === "profile") {
    const p = s.profile;
    return `<h2>Shop profile</h2><p>This information appears on your QR order page and customer receipts.</p>
      <form class="settings-form" data-save-setting="profile"><div class="field-row"><label class="field"><span>Shop name</span><input name="name" maxlength="120" value="${esc(state.owner.shop.name)}" required></label>
      <label class="field"><span>Contact email</span><input name="email" type="email" maxlength="500" value="${esc(p.email || "")}"></label></div>
      <div class="field-row"><label class="field"><span>Phone</span><input name="phone" type="tel" maxlength="500" value="${esc(p.phone || "")}"></label><label class="field"><span>Availability</span><select name="availability">${["open", "busy", "closed"].map((v) => `<option value="${v}" ${p.availability === v ? "selected" : ""}>${esc(pretty(v))}</option>`).join("")}</select></label></div>
      <label class="field"><span>Address</span><textarea name="address" maxlength="500">${esc(p.address || "")}</textarea></label>
      <label class="field"><span>Customer instructions</span><textarea name="instructions" maxlength="500" placeholder="Collection details, counter location, or other helpful notes">${esc(p.instructions || "")}</textarea></label>
      <div class="field-row"><label class="field"><span>Estimated processing (minutes)</span><input name="estimated_minutes" type="number" min="1" max="10080" value="${esc(p.estimated_minutes || "")}" placeholder="Optional"></label>
      <label class="checkbox-line"><input name="service_available" type="checkbox" ${p.service_available ? "checked" : ""}> Accept new online orders</label></div>
      <div class="action-row"><span class="micro">Shop URL: /?shop=${esc(state.owner.shop.slug)}</span><button class="btn" type="submit">Save profile</button></div></form>
      <div class="detail-section"><h2>Shop QR code</h2><div class="qr-row"><img data-shop-qr alt="Shop order QR code" src="/static/icon.svg"><div><strong>${esc(state.owner.shop.name)} order link</strong><p>Print and place this code at your counter. It opens the public ordering page for this shop.</p><button class="btn secondary small" data-download-qr>Download QR SVG</button></div></div></div>`;
  }
  if (state.settingsTab === "pricing") return renderPricingSettings();
  if (state.settingsTab === "general") return renderGeneralSettings();
  return renderPrintingSettings();
}

function renderPricingSettings() {
  const pricing = state.settings.pricing;
  const sizes = state.settings.general.enabled_paper_sizes;
  const rateKeys = [["bw_simplex_per_side", "B&W single side"], ["bw_duplex_per_sheet", "B&W double side / sheet"],
    ["color_simplex_per_side", "Color single side"], ["color_duplex_per_sheet", "Color double side / sheet"]];
  const header = rateKeys.map(([, label]) => `<th>${esc(label)}</th>`).join("");
  const body = sizes.map((size) => `<tr><th>${esc(size)}</th>${rateKeys.map(([key]) => `<td><label class="sr-only">${esc(size)} ${esc(key)}</label><input data-rate-size="${esc(size)}" data-rate-key="${key}" type="number" min="0" step="0.01" required value="${pricing.rates?.[size]?.[key] === undefined ? "" : (pricing.rates[size][key] / 100).toFixed(2)}" aria-label="${esc(size)} ${esc(key)} rate in rupees"></td>`).join("")}</tr>`).join("");
  return `<h2>Print prices</h2><p>Set each billed unit separately. Duplex rates are per physical sheet; simplex rates are per printed side. The server calculates every customer quote.</p>
    <form class="settings-form" data-pricing-form><p class="rate-caption">Prices in Indian rupees</p><div class="table-wrap"><table class="rate-table"><thead><tr><th>Paper</th>${header}</tr></thead><tbody>${body}</tbody></table></div>
      <div class="field-row"><label class="field"><span>Minimum order (₹)</span><input name="minimum_order" type="number" min="0" step="0.01" required value="${(pricing.minimum_order_paise / 100).toFixed(2)}"></label>
      <label class="field"><span>Service fee (₹)</span><input name="service_fee" type="number" min="0" step="0.01" required value="${(pricing.service_fee_paise / 100).toFixed(2)}"></label></div>
      <div class="field-row"><label class="field"><span>Tax (%)</span><input name="tax_rate" type="number" min="0" max="100" step="0.01" required value="${(pricing.tax_rate_bps / 100).toFixed(2)}"></label>
      <label class="field"><span>Discount (%)</span><input name="discount_rate" type="number" min="0" max="100" step="0.01" required value="${((pricing.discount_rate_bps || 0) / 100).toFixed(2)}"></label></div>
      <div class="field-row"><label class="field"><span>Binding (₹ per copy)</span><input name="binding" type="number" min="0" step="0.01" required value="${(pricing.binding_per_copy_paise / 100).toFixed(2)}"></label>
      <label class="field"><span>Stapling (₹ per copy)</span><input name="stapling" type="number" min="0" step="0.01" required value="${(pricing.stapling_per_copy_paise / 100).toFixed(2)}"></label></div>
      ${state.settings.general.enabled_add_ons.length ? `<p class="micro">Enabled add-ons: ${state.settings.general.enabled_add_ons.map(esc).join(", ")}. Their configured per-copy prices apply to orders.</p>` : `<p class="micro">Enable binding or stapling in Customer orders to offer those services.</p>`}
      <div class="surface panel"><div class="admin-panel-head"><div><h2>Pricing preview</h2><p>Try an example using the values in this form before saving.</p></div></div><div class="toolbar"><label class="field"><span>Paper size</span><select data-preview-paper>${sizes.map((size) => `<option>${esc(size)}</option>`).join("")}</select></label><label class="field"><span>Pages</span><input type="number" min="1" value="10" data-preview-pages></label><label class="field"><span>Copies</span><input type="number" min="1" value="1" data-preview-copies></label><label class="field"><span>Mode</span><select data-preview-kind><option value="bw_simplex_per_side">B&W single side</option><option value="bw_duplex_per_sheet">B&W double side</option><option value="color_simplex_per_side">Color single side</option><option value="color_duplex_per_sheet">Color double side</option></select></label></div><p data-pricing-preview class="rate-caption">Enter the rate values to calculate an example.</p></div>
      <div class="action-row"><span class="micro">New rates apply to new orders. Existing order prices are preserved.</span><button class="btn" type="submit">Save prices</button></div></form>`;
}

function renderGeneralSettings() {
  const g = state.settings.general;
  const formats = ["pdf", "jpg", "jpeg", "png", "doc", "docx"];
  const sizes = ["A4", "A3", "LEGAL", "LETTER"];
  return `<h2>Customer orders</h2><p>Control uploads and customer checkout for this shop.</p>
    <form class="settings-form" data-save-setting="general"><div class="field-row three"><label class="field"><span>Maximum file size (MB)</span><input name="max_file_mb" type="number" min="1" max="100" value="${(g.max_file_bytes / (1024 * 1024)).toFixed(0)}" required></label>
    <label class="field"><span>Maximum files per order</span><input name="max_files" type="number" min="1" max="30" value="${g.max_files}" required></label><label class="field"><span>Maximum pages per file</span><input name="max_pages" type="number" min="1" max="5000" value="${g.max_pages}" required></label></div>
    <div class="field-row"><label class="field"><span>File retention (days)</span><input name="retention_days" type="number" min="1" max="90" value="${g.retention_days}" required></label><label class="checkbox-line"><input name="cash_allowed" type="checkbox" ${g.cash_allowed ? "checked" : ""}> Allow payment at the shop counter</label></div>
    <fieldset class="option-box"><legend>Supported customer file types</legend><div class="option-grid">${formats.map((format) => `<label class="checkbox-line"><input type="checkbox" name="format" value="${format}" ${g.formats.includes(format) ? "checked" : ""} ${["doc", "docx"].includes(format) && !g.formats.includes(format) ? "disabled title=\"Requires LibreOffice to be installed on this server\"" : ""}> ${format.toUpperCase()}${["doc", "docx"].includes(format) ? " · LibreOffice required" : ""}</label>`).join("")}</div><p class="micro">Word files are only enabled when the server can convert them with LibreOffice.</p></fieldset>
    <fieldset class="option-box"><legend>Paper sizes customers can select</legend><div class="option-grid">${sizes.map((size) => `<label class="checkbox-line"><input type="checkbox" name="paper" value="${size}" ${g.enabled_paper_sizes.includes(size) ? "checked" : ""}> ${esc(size)}</label>`).join("")}</div></fieldset>
    <fieldset class="option-box"><legend>Extra services</legend><div class="option-grid">${["binding", "stapling"].map((addon) => `<label class="checkbox-line"><input type="checkbox" name="addon" value="${addon}" ${g.enabled_add_ons.includes(addon) ? "checked" : ""}> ${esc(pretty(addon))}</label>`).join("")}</div></fieldset>
    <div class="action-row"><span class="micro">Currency is INR · time zone is Asia/Kolkata</span><button class="btn" type="submit">Save order settings</button></div></form>`;
}

function renderPrintingSettings() {
  const p = state.settings.printing;
  const printers = [...new Set(state.agents.flatMap((agent) => (agent.printers || []).map((printer) => printer.name)))];
  const printerSelect = (name, label, current) => `<label class="field"><span>${esc(label)}</span><select name="${name}"><option value="">Use agent default printer</option>${printers.map((printer) => `<option value="${esc(printer)}" ${current === printer ? "selected" : ""}>${esc(printer)}</option>`).join("")}</select></label>`;
  return `<h2>Printing and approval</h2><p>Manual printing is available without a local agent. Keep automatic jobs disabled until the shop computer has passed a test print.</p>
    <form class="settings-form" data-save-setting="printing"><div class="field-row"><label class="field"><span>Preferred workflow</span><select name="mode"><option value="manual" ${p.mode === "manual" ? "selected" : ""}>Manual dashboard printing</option><option value="automatic" ${p.mode === "automatic" ? "selected" : ""}>Local Print Agent</option></select></label>
    <label class="checkbox-line"><input name="automatic_enabled" type="checkbox" ${p.automatic_enabled ? "checked" : ""}> Allow automatic Print Agent jobs</label></div>
    <label class="checkbox-line"><input name="manual_approval" type="checkbox" ${p.manual_approval ? "checked" : ""}> Require owner approval before a job is sent</label>
    <div class="field-row"><label class="checkbox-line"><input name="approve_color" type="checkbox" ${p.approve_color ? "checked" : ""}> Require extra approval for color jobs</label><label class="checkbox-line"><input name="approve_large_jobs" type="checkbox" ${p.approve_large_jobs ? "checked" : ""}> Require extra approval for large jobs</label></div>
    <div class="field-row">${printerSelect("default_printer", "Black & white / default printer", p.default_printer)}${printerSelect("color_printer", "Color printer", p.color_printer)}</div>
    <div class="field-row three"><label class="field"><span>Large job threshold (pages)</span><input name="large_job_pages" type="number" min="1" max="5000" value="${p.large_job_pages}" required></label><label class="field"><span>Max pages per job</span><input name="max_pages_per_job" type="number" min="1" max="5000" value="${p.max_pages_per_job}" required></label><label class="field"><span>Max copies per file</span><input name="max_copies" type="number" min="1" max="99" value="${p.max_copies}" required></label></div>
    <label class="field"><span>Automatic retry limit</span><input name="retry_limit" type="number" min="0" max="5" value="${p.retry_limit}" required></label>
    <div class="action-row"><span class="micro">A spooler submission remains unverified until a staff member checks the paper output.</span><button class="btn" type="submit">Save print settings</button></div></form>
    <div class="detail-section"><h2>Test the local printer</h2><p class="micro">On the shop computer, run <code>python3 print_agent/agent.py test</code>. The agent discovers the installed printer and sends a clearly labeled test page. Hardware output must be checked at the shop.</p><button class="btn secondary small" data-agent-open>Agent status and pairing</button></div>`;
}

function bindSettingsForm() {
  document.querySelectorAll("[data-save-setting]").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const key = form.dataset.saveSetting;
    try {
      let value;
      if (key === "profile") {
        const data = new FormData(form);
        value = { name: data.get("name").trim(), email: data.get("email").trim(), phone: data.get("phone").trim(),
          address: data.get("address").trim(), instructions: data.get("instructions").trim(), availability: data.get("availability"),
          service_available: data.has("service_available"), estimated_minutes: data.get("estimated_minutes") ? Number(data.get("estimated_minutes")) : null };
      } else if (key === "general") {
        const data = new FormData(form);
        value = { ...state.settings.general, max_file_bytes: Number(data.get("max_file_mb")) * 1024 * 1024,
          max_files: Number(data.get("max_files")), max_pages: Number(data.get("max_pages")), retention_days: Number(data.get("retention_days")),
          cash_allowed: data.has("cash_allowed"), formats: [...new Set(data.getAll("format"))],
          enabled_paper_sizes: data.getAll("paper"), enabled_add_ons: data.getAll("addon") };
      } else if (key === "printing") {
        const data = new FormData(form);
        value = { ...state.settings.printing, mode: data.get("mode"), automatic_enabled: data.has("automatic_enabled"),
          manual_approval: data.has("manual_approval"), approve_color: data.has("approve_color"), approve_large_jobs: data.has("approve_large_jobs"),
          default_printer: data.get("default_printer"), color_printer: data.get("color_printer"), large_job_pages: Number(data.get("large_job_pages")),
          max_pages_per_job: Number(data.get("max_pages_per_job")), max_copies: Number(data.get("max_copies")), retry_limit: Number(data.get("retry_limit")) };
      }
      await saveSetting(key, value);
      if (key === "profile") await refreshOwnerShopName(value.name);
    } catch (error) { toast(error.message, "error"); }
  }));
  document.querySelector("[data-pricing-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const rates = structuredClone(state.settings.pricing.rates || {});
    for (const size of state.settings.general.enabled_paper_sizes) {
      rates[size] ||= {};
      for (const key of ["bw_simplex_per_side", "bw_duplex_per_sheet", "color_simplex_per_side", "color_duplex_per_sheet"]) {
        const raw = form.querySelector(`[data-rate-size="${size}"][data-rate-key="${key}"]`).value;
        const amount = Number(raw);
        if (raw === "" || !Number.isFinite(amount) || amount < 0 || Math.round(amount * 100) !== amount * 100) {
          toast("Enter each print price in rupees with up to two decimal places.", "error"); return;
        }
        rates[size][key] = Math.round(amount * 100);
      }
    }
    const value = { ...state.settings.pricing, rates,
      minimum_order_paise: Math.round(Number(data.get("minimum_order")) * 100),
      service_fee_paise: Math.round(Number(data.get("service_fee")) * 100),
      tax_rate_bps: Math.round(Number(data.get("tax_rate")) * 100),
      discount_rate_bps: Math.round(Number(data.get("discount_rate")) * 100),
      binding_per_copy_paise: Math.round(Number(data.get("binding")) * 100),
      stapling_per_copy_paise: Math.round(Number(data.get("stapling")) * 100) };
    try { await saveSetting("pricing", value); }
    catch (error) { toast(error.message, "error"); }
  });
  document.querySelectorAll("[data-preview-pages], [data-preview-copies], [data-preview-paper], [data-preview-kind], [data-rate-size]").forEach((input) => input.addEventListener("input", renderPricingPreview));
  document.querySelectorAll("[data-preview-paper], [data-preview-kind]").forEach((input) => input.addEventListener("change", renderPricingPreview));
  renderPricingPreview();
  document.querySelector("[data-download-qr]")?.addEventListener("click", () => void downloadShopQr());
  document.querySelector("[data-shop-qr]")?.addEventListener("error", (event) => { event.currentTarget.hidden = true; });
  void loadShopQr();
  document.querySelector("[data-agent-open]")?.addEventListener("click", () => setOwnerView("agent"));
}

async function saveSetting(key, value) {
  await api("/api/admin/settings", { method: "PUT", body: { key, value } });
  toast("Settings saved.", "success");
  await loadSettings();
}

async function refreshOwnerShopName(name) {
  state.owner.shop.name = name;
  renderOwnerShell();
  await loadSettings();
}

function renderPricingPreview() {
  const output = document.querySelector("[data-pricing-preview]");
  const form = document.querySelector("[data-pricing-form]");
  if (!output || !form) return;
  const size = form.querySelector("[data-preview-paper]").value;
  const pages = Math.max(1, Number(form.querySelector("[data-preview-pages]").value) || 1);
  const copies = Math.max(1, Number(form.querySelector("[data-preview-copies]").value) || 1);
  const kind = form.querySelector("[data-preview-kind]").value;
  const rateInput = form.querySelector(`[data-rate-size="${size}"][data-rate-key="${kind}"]`);
  if (!rateInput || !rateInput.value) { output.textContent = "Enter the rate values to calculate an example."; return; }
  const duplex = kind.endsWith("duplex_per_sheet");
  const units = duplex ? Math.ceil(pages / 2) : pages;
  const rawRate = Number(rateInput.value);
  if (!Number.isFinite(rawRate) || rawRate < 0) { output.textContent = "Enter a valid non-negative rate."; return; }
  const pricing = state.settings.pricing;
  const minimum = Number(form.querySelector('[name="minimum_order"]').value || 0);
  const fee = Number(form.querySelector('[name="service_fee"]').value || 0);
  const beforeDiscount = Math.max(minimum, units * copies * rawRate);
  const discountRate = Number(form.querySelector('[name="discount_rate"]').value || 0);
  const discount = beforeDiscount * discountRate / 100;
  const taxable = beforeDiscount - discount + fee;
  const tax = taxable * Number(form.querySelector('[name="tax_rate"]').value || 0) / 100;
  output.textContent = `${pages} pages × ${copies} copies · ${units} billable ${duplex ? "sheets" : "sides"} per copy · ${money(Math.round(beforeDiscount * 100))} before discount · ${money(Math.round(discount * 100))} discount · ${money(Math.round((taxable + tax) * 100))} total (sample)`;
}

async function loadShopQr() {
  const image = document.querySelector("[data-shop-qr]");
  if (!image) return;
  try {
    const response = await fetch("/api/admin/qr.svg", { credentials: "same-origin", headers: { "X-Shop-Slug": state.owner.shop.slug }, cache: "no-store" });
    if (!response.ok) return;
    const url = URL.createObjectURL(await response.blob());
    image.src = url;
    state.qrUrl = url;
  } catch {}
}

async function downloadShopQr() {
  try {
    const response = await fetch("/api/admin/qr.svg", { credentials: "same-origin", headers: { "X-Shop-Slug": state.owner.shop.slug }, cache: "no-store" });
    if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error?.error?.message || "QR code could not be downloaded."); }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${state.owner.shop.slug}-qr.svg`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (error) { toast(error.message, "error"); }
}

async function loadAgents() {
  const result = await api("/api/admin/agents");
  state.agents = result.agents || [];
  const cards = state.agents.map(renderAgentCard).join("");
  const command = `python3 print_agent/agent.py pair --url ${location.origin} --name "Shop printer"`;
  ownerContent(`<section class="surface admin-panel"><div class="admin-panel-head"><div><h2>Paired shop computers</h2><p>The agent uses outbound HTTPS polling; no public printer port is opened.</p></div><button class="btn" data-new-pairing>Generate pairing code</button></div>
    ${state.pairing ? `<div class="notice detail-section"><div><strong>Pairing code expires ${esc(dateTime(state.pairing.expires_at))}</strong><div class="code-box">${esc(state.pairing.code)}</div><p>Run this on the shop computer, then enter the code when prompted:</p><div class="code-box">${esc(command)}</div><button class="btn secondary small" data-copy-pair>Copy command</button></div></div>` : ""}
    ${cards || emptyState("No Print Agent paired", "Generate a one-time code, then pair the agent from the shop computer.")}
    <div class="content-grid equal detail-section"><div class="surface panel"><h2>Install and pair</h2><ol class="plain-list"><li>Copy this project to the Windows or Linux computer connected to the printer.</li><li>Run <code>python3 print_agent/agent.py pair --url ${esc(location.origin)} --name "Shop printer"</code>.</li><li>Run <code>python3 print_agent/agent.py test</code> and check the paper output.</li><li>Enable automatic printing only after the test and a paid sample order are verified.</li></ol><p class="micro">Windows needs Python and SumatraPDF configured through <code>SMX_SUMATRA_PATH</code>. Linux uses CUPS.</p></div>
      <div class="surface panel"><h2>Run the worker</h2><p>After pairing, start the worker with <code>python3 print_agent/agent.py run</code>. It sends a heartbeat and checks for work every few seconds.</p><p class="micro">The worker stores its private device token locally with restrictive file permissions. Revoke any device that is lost or retired.</p><button class="btn secondary small" data-go-print-settings>Review print rules</button></div></div></section>`);
  document.querySelector("[data-new-pairing]")?.addEventListener("click", async () => {
    try { state.pairing = { ...(await api("/api/admin/agents/pairing-code", { method: "POST", body: {} })), code: "" }; state.pairing.code = state.pairing.pairing_code; await loadAgents(); }
    catch (error) { toast(error.message, "error"); }
  });
  document.querySelector("[data-copy-pair]")?.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(command); toast("Pairing command copied.", "success"); }
    catch { toast("Could not copy the command. Select and copy it from the box.", "error"); }
  });
  document.querySelectorAll("[data-revoke-agent]").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("Revoke this Print Agent? The device token will stop working.")) return;
    try { await api(`/api/admin/agents/${button.dataset.revokeAgent}`, { method: "DELETE" }); await loadAgents(); }
    catch (error) { toast(error.message, "error"); }
  }));
  document.querySelector("[data-go-print-settings]")?.addEventListener("click", () => { state.settingsTab = "printing"; setOwnerView("settings"); });
}

function renderAgentCard(agent) {
  const printers = (agent.printers || []).length ? agent.printers.map((printer) => `<div class="printer-line"><span class="status-badge ${printer.status === "ready" ? "completed" : "offline"}">${esc(pretty(printer.status))}</span><span>${esc(printer.name)}${printer.is_default ? " · default" : ""}</span><small>${printer.color === null ? "color unknown" : printer.color ? "color" : "B&W"} · ${printer.duplex === null ? "duplex unknown" : printer.duplex ? "duplex" : "simplex"}</small></div>`).join("") : `<p class="micro">No printer has reported yet. Start the local agent to discover printers.</p>`;
  return `<article class="agent-card"><div class="agent-card-head"><div><strong>${esc(agent.name)}</strong><div class="table-secondary">${esc(agent.version || "Agent version unknown")} · last heartbeat ${esc(dateTime(agent.last_heartbeat_at))}</div></div>${statusBadge(agent.revoked_at ? "revoked" : agent.status)}</div>
    <div class="printer-list">${printers}</div>${agent.active_job_id ? `<p class="micro">Active job: ${esc(agent.active_job_id)}</p>` : ""}${agent.last_error ? ownerNotice(agent.last_error, "error") : ""}
    ${!agent.revoked_at ? `<div class="detail-actions"><button class="btn danger small" data-revoke-agent="${esc(agent.id)}">Revoke device</button></div>` : ""}</article>`;
}

async function loadStaff() {
  const [result, activityResult] = await Promise.all([api("/api/admin/staff"), api("/api/admin/audit")]);
  const staff = result.staff || [];
  const activity = activityResult.activity || [];
  const permissionInputs = (selected = {}) => STAFF_PERMISSIONS.map((permission) => `<label class="checkbox-line"><input type="checkbox" name="permission" value="${permission}" ${selected[permission] ? "checked" : ""}> ${esc(pretty(permission))}</label>`).join("");
  const cards = staff.map((member) => `<article class="agent-card"><div class="agent-card-head"><div><strong>${esc(member.name)}</strong><div class="table-secondary">${esc(member.email)} · added ${esc(dateTime(member.created_at))}</div></div>${statusBadge(member.is_active ? "active" : "offline")}</div>
    <form class="settings-form staff-permissions" data-staff-permissions="${esc(member.id)}"><div class="option-grid">${permissionInputs(member.permissions)}</div><div class="detail-actions"><button class="btn secondary small" type="submit">Save permissions</button><button class="btn subtle small" type="button" data-staff-toggle="${esc(member.id)}" data-enabled="${member.is_active ? "1" : "0"}">${member.is_active ? "Disable access" : "Re-enable access"}</button></div></form></article>`).join("");
  const activityRows = activity.map((item) => {
    const details = Object.entries(item.details || {}).slice(0, 4).map(([key, value]) => `${pretty(key)}: ${String(value)}`).join(" · ");
    return `<tr><td><span class="table-primary">${esc(pretty(item.action))}</span><div class="table-secondary">${esc(pretty(item.entity_type))}${item.entity_id ? ` · ${esc(item.entity_id.slice(0, 8))}` : ""}</div></td><td>${esc(item.actor_name || "System")}${details ? `<div class="table-secondary">${esc(details)}</div>` : ""}</td><td>${esc(dateTime(item.created_at))}</td></tr>`;
  }).join("");
  ownerContent(`<section class="surface admin-panel"><div class="admin-panel-head"><div><h2>Add staff login</h2><p>Staff can only use the permissions you select for this shop.</p></div></div>
    <form class="settings-form" data-create-staff><div class="field-row three"><label class="field"><span>Full name</span><input name="name" maxlength="120" required></label><label class="field"><span>Email address</span><input name="email" type="email" required></label><label class="field"><span>Temporary password</span><input name="password" type="password" minlength="12" autocomplete="new-password" required></label></div><fieldset class="option-box"><legend>Permissions</legend><div class="option-grid">${permissionInputs({ "orders.view": true })}</div></fieldset><div class="action-row"><span class="micro">Share the temporary password securely and ask staff to change it.</span><button class="btn" type="submit">Create staff login</button></div></form></section>
    <section class="surface admin-panel detail-section spaced"><div class="admin-panel-head"><div><h2>Current staff</h2><p>${staff.length} staff account${staff.length === 1 ? "" : "s"} assigned to this shop.</p></div><button class="btn secondary small" data-refresh-staff>Refresh</button></div>${cards || emptyState("No staff logins", "Add a team member when someone else needs to handle orders.")}</section>
    <section class="surface admin-panel detail-section spaced"><div class="admin-panel-head"><div><h2>Recent activity</h2><p>Last ${activity.length} changes to orders, staff access, payments, and shop settings.</p></div></div>${activityRows ? `<div class="table-wrap"><table><thead><tr><th>Change</th><th>By</th><th>When</th></tr></thead><tbody>${activityRows}</tbody></table></div>` : emptyState("No activity yet", "Shop changes will appear here.")}</section>`);
  document.querySelector("[data-create-staff]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget, data = new FormData(form);
    const permissions = Object.fromEntries(STAFF_PERMISSIONS.map((key) => [key, data.getAll("permission").includes(key)]));
    try {
      await api("/api/admin/staff", { method: "POST", body: { name: data.get("name"), email: data.get("email"), password: data.get("password"), permissions } });
      toast("Staff login created.", "success"); await loadStaff();
    } catch (error) { toast(error.message, "error"); }
  });
  document.querySelectorAll("[data-staff-permissions]").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form), permissions = Object.fromEntries(STAFF_PERMISSIONS.map((key) => [key, data.getAll("permission").includes(key)]));
    try { await api(`/api/admin/staff/${form.dataset.staffPermissions}`, { method: "PATCH", body: { permissions } }); toast("Staff permissions saved.", "success"); }
    catch (error) { toast(error.message, "error"); }
  }));
  document.querySelectorAll("[data-staff-toggle]").forEach((button) => button.addEventListener("click", async () => {
    try { await api(`/api/admin/staff/${button.dataset.staffToggle}`, { method: "PATCH", body: { enabled: button.dataset.enabled !== "1" } }); await loadStaff(); }
    catch (error) { toast(error.message, "error"); }
  }));
  document.querySelector("[data-refresh-staff]")?.addEventListener("click", () => void loadStaff());
}

async function loadPlatformView() {
  if (state.owner.user.global_role !== "super_admin") { ownerContent(ownerNotice("Platform administrator access is required.", "error")); return; }
  const [shops, stats] = await Promise.all([api("/api/super-admin/shops"), api("/api/super-admin/analytics")]);
  state.owner.shops = shops.shops;
  const a = stats.analytics;
  const rows = shops.shops.map((shop) => `<tr><td><span class="table-primary">${esc(shop.name)}</span><div class="table-secondary">/${esc(shop.slug)} · created ${esc(dateTime(shop.created_at))}</div></td><td>${shop.order_count}</td><td>${statusBadge(shop.status)}</td><td><div class="detail-actions"><button class="btn secondary small" data-manage-shop="${esc(shop.slug)}">Manage shop</button><button class="btn ${shop.status === "active" ? "danger" : "secondary"} small" data-shop-action="${shop.status === "active" ? "suspend" : "activate"}" data-shop-id="${esc(shop.id)}">${shop.status === "active" ? "Suspend" : "Activate"}</button></div></td></tr>`).join("");
  ownerContent(`<div class="stats-grid"><article class="surface stat-card"><div class="stat-top">Registered shops</div><div class="stat-value">${a.shops}</div></article><article class="surface stat-card"><div class="stat-top">Active shops</div><div class="stat-value">${a.active_shops}</div></article><article class="surface stat-card"><div class="stat-top">Platform orders</div><div class="stat-value">${a.orders}</div></article><article class="surface stat-card"><div class="stat-top">Verified revenue</div><div class="stat-value">${money(a.revenue_paise)}</div></article></div>
    <section class="surface admin-panel"><div class="admin-panel-head"><div><h2>Registered shops</h2><p>Create an isolated shop workspace or temporarily suspend customer checkout.</p></div><button class="btn secondary small" data-create-shop-open>Create shop</button></div>
    ${rows ? `<div class="table-wrap"><table><thead><tr><th>Shop</th><th>Orders</th><th>State</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>` : emptyState("No shops yet", "Create a shop account to start onboarding.")}</section>`);
  document.querySelector("[data-create-shop-open]")?.addEventListener("click", showCreateShopModal);
  document.querySelectorAll("[data-manage-shop]").forEach((button) => button.addEventListener("click", () => {
    state.owner.shop = state.owner.shops.find((shop) => shop.slug === button.dataset.manageShop);
    state.ownerView = "overview"; renderOwnerShell(); void loadOwnerView();
  }));
  document.querySelectorAll("[data-shop-action]").forEach((button) => button.addEventListener("click", async () => {
    const verb = button.dataset.shopAction;
    if (verb === "suspend" && !confirm("Suspend this shop? Customers will not be able to place new orders.")) return;
    try { await api(`/api/super-admin/shops/${button.dataset.shopId}/${verb}`, { method: "POST", body: {} }); await loadPlatformView(); }
    catch (error) { toast(error.message, "error"); }
  }));
}

function showCreateShopModal() {
  showModal("Create a shop", `<p class="micro">The new shop gets its own owner account, order records, settings, files, and QR URL.</p><form class="settings-form" data-create-shop>
    <label class="field"><span>Shop name</span><input name="name" maxlength="120" required></label><div class="field-row"><label class="field"><span>Public URL slug</span><input name="slug" pattern="[a-z0-9]+(-[a-z0-9]+)*" placeholder="campus-copy" required></label><label class="field"><span>Contact phone</span><input name="phone"></label></div>
    <label class="field"><span>Address</span><input name="address"></label><div class="field-row"><label class="field"><span>Owner name</span><input name="owner_name" required></label><label class="field"><span>Owner email</span><input name="owner_email" type="email" required></label></div><label class="field"><span>Temporary password (12+ characters)</span><input name="owner_password" type="password" minlength="12" required></label>
    <div class="detail-actions"><button class="btn secondary" type="button" data-modal-close>Cancel</button><button class="btn" type="submit">Create shop</button></div></form>`);
  document.querySelector("[data-modal-close]")?.addEventListener("click", closeModal);
  document.querySelector("[data-create-shop]")?.addEventListener("submit", async (event) => {
    event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget));
    try { await api("/api/super-admin/shops", { method: "POST", body: data }); closeModal(); toast("Shop created.", "success"); await startOwner(); }
    catch (error) { toast(error.message, "error"); }
  });
}

function showModal(title, content) {
  closeModal();
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop"; backdrop.setAttribute("role", "presentation");
  backdrop.innerHTML = `<section class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}"><div class="modal-head"><h2>${esc(title)}</h2><button type="button" class="icon-btn" aria-label="Close dialog" data-modal-close>×</button></div>${content}</section>`;
  document.body.append(backdrop); state.modal = backdrop;
  backdrop.addEventListener("click", (event) => { if (event.target === backdrop) closeModal(); });
  backdrop.querySelector("[data-modal-close]")?.addEventListener("click", closeModal);
  backdrop.querySelector("input,button,select")?.focus();
}

function closeModal() {
  state.modal?.remove(); state.modal = null;
}

async function refreshNotifications() {
  if (!state.owner?.shop) return;
  try {
    const result = await api("/api/admin/notifications");
    state.notifications = result.notifications || [];
    const unread = state.notifications.filter((item) => !item.read_at).length;
    const badge = document.querySelector("[data-unread-count]");
    if (badge) badge.textContent = unread ? `· ${unread}` : "";
  } catch {}
}

async function showNotifications() {
  if (!state.owner?.shop) { toast("Select a shop to view its notifications."); return; }
  try {
    const result = await api("/api/admin/notifications"); state.notifications = result.notifications || [];
    const content = result.notifications.length ? result.notifications.map((item) => `<article class="agent-card"><div class="agent-card-head"><strong>${esc(item.title)}</strong>${item.read_at ? "" : `<span class="status-badge pending">New</span>`}</div><p>${esc(item.message)}</p><small class="micro">${esc(dateTime(item.created_at))}</small>${!item.read_at ? `<button class="btn subtle small" data-read-notification="${esc(item.id)}">Mark read</button>` : ""}</article>`).join("") : emptyState("No notifications", "Shop updates will appear here.");
    showModal("Shop notifications", content);
    document.querySelectorAll("[data-read-notification]").forEach((button) => button.addEventListener("click", async () => {
      try { await api(`/api/admin/notifications/${button.dataset.readNotification}/read`, { method: "POST", body: {} }); await showNotifications(); await refreshNotifications(); }
      catch (error) { toast(error.message, "error"); }
    }));
  } catch (error) { toast(error.message, "error"); }
}

function startApplication() {
  if (location.pathname.startsWith("/admin")) {
    void startOwner();
    clearInterval(state.ownerPoll);
    state.ownerPoll = setInterval(() => {
      if (!state.owner || !["overview", "orders", "queue", "payments"].includes(state.ownerView) || state.modal) return;
      void loadOwnerView(); void refreshNotifications();
    }, 15000);
  } else if (location.pathname.startsWith("/track")) {
    void renderTrackingPage();
    clearInterval(state.trackPoll);
    state.trackPoll = setInterval(() => { if (!document.querySelector("[data-open-track]")) void renderTrackingPage(); }, 15000);
  } else {
    void startCustomer();
  }
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeModal(); });
}

startApplication();
