const $ = (id) => document.getElementById(id);

chrome.storage.sync.get(["enabled", "streng"], (s) => {
  $("enabled").checked = s.enabled !== false;       // default på
  $("streng").value = s.streng || "medium";
});
chrome.storage.local.get(["hiddenCount"], (s) => {
  $("count").textContent = s.hiddenCount || 0;
});

$("enabled").addEventListener("change", (e) =>
  chrome.storage.sync.set({ enabled: e.target.checked }));
$("streng").addEventListener("change", (e) =>
  chrome.storage.sync.set({ streng: e.target.value }));
