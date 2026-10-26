"use strict";

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="sync-token"]').content;
let snapshot = null;
let repositories = [];

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function message(text, kind = "") {
  $("message").textContent = text;
  $("message").className = kind;
}

function busy(value) {
  $("repository").disabled = value || !repositories.length;
  $("refresh").disabled = value || !repositories.length;
  $("sync").disabled = value || snapshot?.status !== "BEHIND";
}

async function api(action) {
  const response = await fetch("/api/" + action, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Sync-Token": token },
    body: JSON.stringify({ repository_id: $("repository").value }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

function renderCommits(id, commits) {
  const list = $(id);
  list.replaceChildren();
  if (!commits.length) {
    list.append(element("li", "No commits unique to this side.", "empty"));
    return;
  }
  for (const commit of commits) {
    const item = element("li");
    item.append(element("span", commit.subject, "commit-title"));
    const meta = element("div", undefined, "commit-meta");
    meta.append(element("span", commit.author + " · " + commit.date.slice(0, 10)),
      element("code", commit.sha.slice(0, 10)));
    item.append(meta);
    list.append(item);
  }
}

function renderPatch(details, file) {
  if (file.binary) {
    details.append(element("p", "Binary file changed. Text preview is unavailable.", "file-note"));
    return;
  }
  const pre = element("pre", undefined, "patch");
  const fragment = document.createDocumentFragment();
  for (const line of file.patch.split("\n")) {
    let style = "";
    if (line.startsWith("+") && !line.startsWith("+++")) style = " added";
    if (line.startsWith("-") && !line.startsWith("---")) style = " removed";
    if (line.startsWith("@@")) style = " hunk";
    fragment.append(element("span", line, "diff-line" + style));
  }
  pre.append(fragment);
  details.append(pre);
  if (file.truncated) details.append(element("p",
    "Preview truncated. Inspect the complete patch with Git.", "file-note"));
}

function renderFiles(data) {
  const list = $("files");
  list.replaceChildren();
  if (!data.files.length) list.append(element("p", "The two commits have identical file contents.", "empty"));
  for (const file of data.files) {
    const details = element("details", undefined, "file");
    const summary = element("summary");
    const stats = element("span", undefined, "stats");
    stats.append(element("span", "+" + file.added, "additions"),
      element("span", "−" + file.deleted, "deletions"));
    summary.append(element("code", file.path), stats);
    details.append(summary);
    let rendered = false;
    details.addEventListener("toggle", () => {
      if (details.open && !rendered) {
        renderPatch(details, file);
        rendered = true;
      }
    });
    list.append(details);
  }
  $("file-limit").textContent = data.total_files > data.file_limit
    ? "Showing the first " + data.file_limit + " of " + data.total_files + " changed files." : "";
}

function render(data) {
  snapshot = data;
  const states = {
    BEHIND: ["Upstream changes are ready to sync", "Your local branch can be fast-forwarded.", "ready"],
    UP_TO_DATE: ["Up to date", "Your local branch matches the fetched upstream branch.", "current"],
    AHEAD: ["Your local fork is ahead", "Local commits require review. Automatic synchronization is unavailable.", "review"],
    DIVERGED: ["Branches have diverged", "Both sides contain unique commits. Review the differences before integrating them.", "review"],
    UNRELATED: ["Histories are unrelated", "These branches have no shared ancestor. Manual review is required.", "review"],
  };
  const [title, description, style] = states[data.status];
  $("state-title").textContent = title;
  $("state-description").textContent = description;
  $("banner").className = "banner " + style;
  $("counts").textContent = data.ahead + " commits ahead · " + data.behind + " commits behind upstream";
  for (const side of ["local", "upstream"]) {
    $(side + "-sha").textContent = data[side + "_sha"].slice(0, 12);
    $(side + "-sha").title = data[side + "_sha"];
    renderCommits(side + "-commits", data[side + "_commits"]);
  }
  $("upstream-heading").textContent = "Only in upstream (" + data.behind + ")";
  $("local-heading").textContent = "Only in local fork (" + data.ahead + ")";
  $("commit-count").textContent = data.ahead + data.behind;
  $("file-count").textContent = data.total_files;
  $("commit-limit").textContent = Math.max(data.ahead, data.behind) > data.commit_limit
    ? "Showing up to " + data.commit_limit + " commits per side. Counts include all commits." : "";
  renderFiles(data);
  $("comparison").hidden = false;
}

async function refreshComparison(notice = "") {
  snapshot = null;
  busy(true);
  $("comparison").hidden = true;
  const config = repositories.find((repo) => repo.id === $("repository").value);
  $("branches").textContent = config.branch + " ← " + config.upstream + "/" + config.branch;
  message((notice ? notice + " " : "") + "Fetching upstream comparison…");
  try {
    render(await api("compare"));
    message(notice || "Comparison refreshed at " + new Date().toLocaleTimeString() + ".", notice ? "success" : "");
  } catch (error) {
    message((notice ? notice + " " : "") + "Unable to refresh: " + error.message, "error");
  } finally {
    busy(false);
  }
}

$("refresh").addEventListener("click", () => refreshComparison());
$("repository").addEventListener("change", () => refreshComparison());
$("sync").addEventListener("click", async () => {
  busy(true);
  message("Synchronizing the local checkout…");
  try {
    const result = await api("sync");
    const notices = {
      UPDATED: "Local checkout updated to " + result.current_sha.slice(0, 12) + ".",
      UP_TO_DATE: "The local checkout is already up to date.",
      NEEDS_REVIEW: "Repository state changed; manual review is now required.",
    };
    await refreshComparison(notices[result.status]);
  } catch (error) {
    snapshot = null;
    message("Synchronization stopped: " + error.message + ". Refresh after resolving the issue.", "error");
  } finally {
    busy(false);
  }
});

function selectTab(name) {
  for (const tab of ["commits", "files"]) {
    $(tab + "-tab").setAttribute("aria-selected", String(tab === name));
    $(tab + "-tab").tabIndex = tab === name ? 0 : -1;
    $(tab + "-panel").hidden = tab !== name;
  }
}
for (const tab of ["commits", "files"]) {
  $(tab + "-tab").addEventListener("click", () => selectTab(tab));
  $(tab + "-tab").addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const next = tab === "commits" ? "files" : "commits";
      selectTab(next);
      $(next + "-tab").focus();
    }
  });
}

async function start() {
  try {
    const response = await fetch("/api/repositories");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to load repositories");
    repositories = data.repositories;
    $("repository").replaceChildren(...repositories.map((repo) => {
      const option = element("option", repo.id);
      option.value = repo.id;
      return option;
    }));
    await refreshComparison();
  } catch (error) {
    message(error.message, "error");
  }
}
selectTab("commits");
start();
