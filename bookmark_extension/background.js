const APP_ORIGIN = "http://127.0.0.1:8765";
const DESTINATION = [
  "My space",
  "SITES",
  "Cryptocurrency Forums",
  "완료 똥코인",
];

function walkBookmarks(nodes) {
  const bookmarks = [];
  for (const node of nodes || []) {
    if (node.url) bookmarks.push({ title: node.title, url: node.url });
    else bookmarks.push(...walkBookmarks(node.children));
  }
  return bookmarks;
}

async function getTemporaryBookmarks() {
  const tree = await chrome.bookmarks.getTree();
  const folders = [];
  function findFolders(nodes, parentPath = []) {
    for (const node of nodes || []) {
      const path = node.title ? [...parentPath, node.title] : parentPath;
      if (!node.url && node.title === "임시코인") folders.push({ node, path });
      findFolders(node.children, path);
    }
  }
  findFolders(tree);
  if (folders.length !== 1) {
    throw new Error(
      folders.length
        ? `임시코인 폴더가 여러 개 있습니다: ${folders.map((item) => item.path.join(" > ")).join(" / ")}`
        : "Chrome 북마크에서 임시코인 폴더를 찾을 수 없습니다."
    );
  }
  return walkBookmarks(folders[0].node.children);
}

async function destinationFolder() {
  const tree = await chrome.bookmarks.getTree();
  const roots = tree[0]?.children || [];
  const matches = [];
  function findPaths(nodes) {
    for (const node of nodes || []) {
      if (!node.url && node.title === DESTINATION[0]) {
        let current = node;
        for (const title of DESTINATION.slice(1)) {
          current = (current.children || []).find(
            (child) => !child.url && child.title === title
          );
          if (!current) break;
        }
        if (current) matches.push(current);
      }
      findPaths(node.children);
    }
  }
  findPaths(roots);
  if (matches.length > 1) {
    throw new Error("완료 똥코인 대상 폴더가 여러 경로에 있어 임의로 선택할 수 없습니다.");
  }
  if (matches.length === 1) return matches[0];

  let parent = roots.find((node) => node.id === "1") || roots[0];
  if (!parent) throw new Error("Chrome 북마크 루트를 찾을 수 없습니다.");
  for (const title of DESTINATION) {
    let child = (parent.children || []).find(
      (node) => !node.url && node.title === title
    );
    if (!child) child = await chrome.bookmarks.create({ parentId: parent.id, title });
    parent = child;
  }
  return parent;
}

async function addCoinBookmarks(entries) {
  if (!Array.isArray(entries)) throw new Error("북마크 입력 형식이 올바르지 않습니다.");
  const folder = await destinationFolder();
  const existing = await chrome.bookmarks.getChildren(folder.id);
  const existingUrls = new Set(existing.filter((item) => item.url).map((item) => item.url));
  const added = [];
  const alreadyPresent = [];
  for (const entry of entries) {
    const url = String(entry?.url || "");
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      continue;
    }
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      !/(^|\.)coingecko\.com$/i.test(parsed.hostname)
    ) {
      continue;
    }
    if (existingUrls.has(url)) {
      alreadyPresent.push(url);
      continue;
    }
    await chrome.bookmarks.create({
      parentId: folder.id,
      title: String(entry?.title || url).slice(0, 180),
      url,
    });
    existingUrls.add(url);
    added.push(url);
  }
  return { added, already_present: alreadyPresent, folder: DESTINATION };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (
    message?.channel !== "coingecko-archive-bookmarks-request" ||
    !sender.url ||
    new URL(sender.url).origin !== APP_ORIGIN
  ) {
    return false;
  }
  const action =
    message.action === "get-temporary"
      ? getTemporaryBookmarks()
      : message.action === "add-coins"
        ? addCoinBookmarks(message.entries)
        : Promise.reject(new Error("지원하지 않는 북마크 작업입니다."));
  action.then(
    (result) => sendResponse({ requestId: message.requestId, result }),
    (error) =>
      sendResponse({
        requestId: message.requestId,
        error: error instanceof Error ? error.message : String(error),
      })
  );
  return true;
});
