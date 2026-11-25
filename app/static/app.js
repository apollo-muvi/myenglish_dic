const form = document.querySelector("#lookup-form");
const input = document.querySelector("#word-input");
const button = document.querySelector("#search-button");
const result = document.querySelector("#result");
const statusBox = document.querySelector("#status");
const savedPanel = document.querySelector("#saved-panel");
const SAVED_KEY = "myenglish-dic-saved-v1";
const RECENT_KEY = "myenglish-dic-recent-v1";
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const readList = (key) => { try { const value = JSON.parse(localStorage.getItem(key) || "[]"); return Array.isArray(value) ? value : []; } catch { return []; } };
let savedCache = readList(SAVED_KEY);
const savedWords = () => savedCache;
const isSaved = (word) => savedWords().some((item) => item.word.toLocaleLowerCase() === word.toLocaleLowerCase());

async function savedApi(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "收藏同步失敗，請稍後再試。");
  return data;
}

function setSavedWords(words) {
  savedCache = words;
  localStorage.setItem(SAVED_KEY, JSON.stringify(words));
  renderSaved();
}

async function syncSavedWords() {
  try {
    let serverWords = (await savedApi("/api/saved")).words;
    const known = new Set(serverWords.map((word) => word.toLocaleLowerCase()));
    for (const item of readList(SAVED_KEY)) {
      const word = typeof item?.word === "string" ? item.word.trim() : "";
      if (!word || known.has(word.toLocaleLowerCase())) continue;
      try {
        await savedApi("/api/saved", {
          method: "POST",
          body: JSON.stringify({ word }),
        });
        known.add(word.toLocaleLowerCase());
      } catch {
        // Ignore invalid legacy entries and continue migrating the remaining words.
      }
    }
    serverWords = (await savedApi("/api/saved")).words;
    setSavedWords(serverWords.map((word) => ({ word })));
    return true;
  } catch (error) {
    renderSaved();
    statusBox.textContent = error.message || "無法載入伺服器收藏，請重新整理再試。";
    statusBox.hidden = false;
    return false;
  }
}

function linkWords(sentence) {
  return escapeHTML(sentence).replace(/\b[A-Za-z]+(?:['’][A-Za-z]+)?\b/g, (word) => `<button class="inline-word" type="button" data-word="${escapeHTML(word)}">${word}</button>`);
}

function renderSaved() {
  const words = savedWords();
  document.querySelector("#saved-count").textContent = String(words.length);
  document.querySelector("#saved-words").innerHTML = words.length
    ? words.map((item) => `<button class="word-chip" type="button" data-word="${escapeHTML(item.word)}">${escapeHTML(item.word)}</button>`).join("")
    : `<span class="muted">還沒有收藏單字。</span>`;
}

function renderRecent() {
  const words = readList(RECENT_KEY);
  document.querySelector("#recent-words").innerHTML = words.length
    ? `<span class="recent-label">最近查詢</span>${words.map((word) => `<button class="recent-chip" type="button" data-word="${escapeHTML(word)}">${escapeHTML(word)}</button>`).join("")}`
    : "";
}

function rememberWord(word) {
  const words = [word, ...readList(RECENT_KEY).filter((item) => item.toLocaleLowerCase() !== word.toLocaleLowerCase())].slice(0, 8);
  localStorage.setItem(RECENT_KEY, JSON.stringify(words));
  renderRecent();
}

function speak(text, accent) {
  if (!("speechSynthesis" in window)) {
    statusBox.textContent = "此瀏覽器不支援語音播放。";
    statusBox.hidden = false;
    return;
  }
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = accent === "uk" ? "en-GB" : "en-US";
  const voices = window.speechSynthesis.getVoices();
  const voice = voices.find((item) => item.lang.toLowerCase() === utterance.lang.toLowerCase());
  if (voice) utterance.voice = voice;
  window.speechSynthesis.speak(utterance);
}

function renderEntry(data) {
  const entry = data.entry;
  const examples = entry.examples.map((item) => `<li class="example"><div class="example-top"><span class="tense">${escapeHTML(item.tense)}</span><button class="audio-button" type="button" data-speak="${escapeHTML(item.sentence)}" data-accent="us" aria-label="播放例句">▶ 播放</button></div><p>${linkWords(item.sentence)}</p><div class="translation">${escapeHTML(item.translation_zh)}</div><small class="usage">${escapeHTML(item.usage_note)}</small></li>`).join("");
  const similar = data.similar_spelling.length
    ? `<div class="word-chips">${data.similar_spelling.map((word) => `<button class="word-chip" type="button" data-word="${escapeHTML(word)}">${escapeHTML(word)}</button>`).join("")}</div>`
    : `<p class="empty-similar">目前沒有找到拼字相近的常用單字。</p>`;
  const synonyms = entry.synonym_comparison;
  const synonymMarkup = synonyms ? `<section class="extra-section"><h3 class="section-title">同義字比較</h3>${synonyms.core_distinction_zh ? `<p class="extra-intro">${escapeHTML(synonyms.core_distinction_zh)}</p>` : ""}<div class="extra-grid">${synonyms.synonyms.map((item) => `<article class="extra-card"><button class="extra-word" type="button" data-word="${escapeHTML(item.word)}">${escapeHTML(item.word)}</button><p>${escapeHTML(item.meaning_zh)}</p><p>${item.register === "spoken" ? "日常口語" : "偏書面／正式"} · 使用頻率：${({ high: "高", medium: "中", low: "低" })[item.frequency] || "一般"}</p><p>${escapeHTML(item.distinction_zh)}</p><p class="example-sentence">${linkWords(item.example)}</p><p class="translation">${escapeHTML(item.translation_zh)}</p></article>`).join("")}</div></section>` : "";
  const family = entry.word_family?.forms;
  const familyMarkup = family ? `<section class="extra-section"><h3 class="section-title">同字根的其他詞性</h3><div class="extra-grid">${family.map((item) => `<article class="extra-card"><div class="family-heading"><button class="extra-word" type="button" data-word="${escapeHTML(item.word)}">${escapeHTML(item.word)}</button><span>${escapeHTML(item.part_of_speech)}</span></div><p>${escapeHTML(item.meaning_zh)}</p><p>${escapeHTML(item.pattern)}</p><p class="example-sentence">${linkWords(item.example)}</p><p class="translation">${escapeHTML(item.translation_zh)}</p></article>`).join("")}</div></section>` : "";
  const word = entry.word;
  result.innerHTML = `<article class="card"><div class="word-heading"><div><p class="part-of-speech">${escapeHTML(entry.part_of_speech)}</p><h2>${escapeHTML(word)}</h2></div><button class="save-button ${isSaved(input.value.trim()) ? "is-saved" : ""}" type="button" data-save-word="${escapeHTML(input.value.trim())}">${isSaved(input.value.trim()) ? "★ 已收藏" : "☆ 收藏"}</button></div><div class="pronunciation-row"><span>英式 ${escapeHTML(entry.pronunciation_uk)}<br>美式 ${escapeHTML(entry.pronunciation_us)}</span><div class="pronunciation-actions"><button class="audio-button" type="button" data-speak="${escapeHTML(word)}" data-accent="uk">▶ 英式</button><button class="audio-button" type="button" data-speak="${escapeHTML(word)}" data-accent="us">▶ 美式</button></div></div><p class="definition-en">${escapeHTML(entry.definition_en)}</p><p class="definition-zh">${escapeHTML(entry.definition_zh)}</p><h3 class="section-title">生活例句 · 點字繼續查詢</h3><ol class="examples">${examples}</ol><h3 class="section-title">拼字相近的單字</h3>${similar}${familyMarkup}${synonymMarkup}</article>`;
}

async function lookup(word) {
  input.value = word;
  statusBox.hidden = true;
  statusBox.textContent = "";
  result.innerHTML = `<div class="loading" role="status">正在查詢「${escapeHTML(word)}」…</div>`;
  button.disabled = true;
  try {
    await savedReady;
    const response = await fetch("/api/lookup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ word, include_synonyms: document.querySelector("#include-synonyms").checked, include_word_family: document.querySelector("#include-word-family").checked }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "查詢失敗，請稍後重試。");
    rememberWord(word);
    renderEntry(data);
  } catch (error) {
    result.innerHTML = "";
    statusBox.textContent = error.message || "網路連線失敗，請稍後重試。";
    statusBox.hidden = false;
  } finally {
    button.disabled = false;
  }
}

form.addEventListener("submit", (event) => { event.preventDefault(); if (input.value.trim()) lookup(input.value.trim()); });
document.querySelector("#saved-toggle").addEventListener("click", () => {
  const opening = savedPanel.hidden;
  savedPanel.hidden = !opening;
  document.querySelector("#saved-toggle").setAttribute("aria-expanded", String(opening));
});
document.querySelector("#saved-close").addEventListener("click", () => {
  savedPanel.hidden = true;
  document.querySelector("#saved-toggle").setAttribute("aria-expanded", "false");
});
result.addEventListener("click", async (event) => {
  const candidate = event.target.closest("[data-word]");
  if (candidate) return lookup(candidate.dataset.word);
  const audio = event.target.closest("[data-speak]");
  if (audio) return speak(audio.dataset.speak, audio.dataset.accent);
  const save = event.target.closest("[data-save-word]");
  if (save) {
    if (!(await savedReady)) return;
    const word = save.dataset.saveWord;
    const currentlySaved = isSaved(word);
    try {
      if (currentlySaved) {
        await savedApi(`/api/saved/${encodeURIComponent(word)}`, { method: "DELETE" });
      } else {
        await savedApi("/api/saved", {
          method: "POST",
          body: JSON.stringify({ word }),
        });
      }
      const next = currentlySaved
        ? savedWords().filter((item) => item.word.toLocaleLowerCase() !== word.toLocaleLowerCase())
        : [{ word }, ...savedWords()];
      setSavedWords(next);
      statusBox.hidden = true;
    } catch (error) {
      statusBox.textContent = error.message || "收藏儲存失敗，請再試一次。";
      statusBox.hidden = false;
      return;
    }
    const current = result.querySelector(".save-button");
    current.classList.toggle("is-saved", isSaved(word));
    current.textContent = isSaved(word) ? "★ 已收藏" : "☆ 收藏";
  }
});
document.querySelector("#saved-words").addEventListener("click", (event) => { const item = event.target.closest("[data-word]"); if (item) lookup(item.dataset.word); });
document.querySelector("#recent-words").addEventListener("click", (event) => { const item = event.target.closest("[data-word]"); if (item) lookup(item.dataset.word); });
const savedReady = syncSavedWords();
renderSaved();
renderRecent();
const initialWord = new URLSearchParams(location.search).get("word");
if (initialWord) lookup(initialWord);
