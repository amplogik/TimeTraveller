"""Help / About dialog. Two tabs, scrollable, with a find bar across both."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget,
    QTextBrowser, QVBoxLayout,
)


_CSS = """
<style>
    h2 { margin-top: 18px; }
    h3 { margin-top: 14px; }
    p, li { line-height: 1.4; }
    li { margin-bottom: 4px; }
    code { background: rgba(128, 128, 128, 0.18); padding: 1px 4px;
           border-radius: 3px; font-family: monospace; }
    pre { background: rgba(128, 128, 128, 0.12); padding: 8px;
          border-radius: 4px; }
    pre code { background: transparent; padding: 0; }
</style>
"""


_HELP_HTML = _CSS + """
<h2>What is a plan?</h2>
<p>A plan is a saved configuration describing one backup job &mdash; which
directories to back up, where to write them, when to run, and how many
cycles to keep. Each plan lives in its own YAML file under
<code>~/.config/timetraveller/</code>. The <b>Backup Plans</b> sidebar on
the left shows every plan TimeTraveller can see; clicking one loads it
into the editor tabs on the right.</p>

<h2>Plan types</h2>
<p>TimeTraveller has two plan types:</p>
<ul>
<li><b>Active plans</b> run on a schedule (e.g. weekly fulls + daily
incrementals) and prune old cycles automatically when they exceed your
retention policy. Use these for data that changes regularly.</li>
<li><b>Archive plans</b> are not scheduled. You run them manually
whenever you want to capture a new snapshot, and <code>keep_all</code>
retention means cycles never expire. Use these for write-once-read-rarely
data: LLM model checkpoints, a music library, container images.</li>
</ul>
<p>You can flip a plan between types via the <b>Change&hellip;</b> button on
the Plan tab. The dialog explains what will happen &mdash; for
Active&rarr;Archive, all cycles except the newest are deleted
(irreversible).</p>

<h2>How to create a new plan</h2>
<ol>
<li>Click <b>+ New plan&hellip;</b> in the top-right of the toolbar.</li>
<li>Pick a starting template:
    <ul>
    <li><b>home (default)</b> &mdash; covers <code>/home</code> with common
    cache excludes pre-filled.</li>
    <li><b>system (default)</b> &mdash; covers <code>/</code> and
    <code>/boot/efi</code>, with pseudo-filesystem and <code>/home</code>
    excludes pre-filled.</li>
    <li><b>custom&hellip;</b> &mdash; pick your own plan name; starts from
    a home-like template.</li>
    </ul>
</li>
<li>The new plan appears in the sidebar and is loaded into the editor.
Tweak sources, excludes, destination, and retention.</li>
<li>Click <b>Save Plan</b> to write it to disk.</li>
<li>If you want it scheduled, switch to the <b>Schedule</b> tab, pick
weekly or monthly, set the cadence, and click <b>Install schedule</b>
&mdash; this writes the cron entries.</li>
</ol>

<h2>How to view or edit a plan</h2>
<ol>
<li>Click the plan's name in the <b>Backup Plans</b> sidebar.</li>
<li>The three tabs in the centre show its current state:
    <ul>
    <li><b>Plan</b> &mdash; sources, excludes, destination, retention,
    mount options. The "Plan type: Active / Archive" row at the top shows
    its category.</li>
    <li><b>Schedule</b> &mdash; when fulls and incrementals run (or, for
    Archive plans, a notice that no schedule is installed).</li>
    <li><b>Archives</b> &mdash; the cycles already on disk for this
    plan.</li>
    </ul>
</li>
<li>Edit fields in place; the title bar shows
"&bull; unsaved changes" until you click <b>Save Plan</b>.</li>
</ol>

<h2>How to browse an archive</h2>
<ol>
<li>Pick a plan in the sidebar, then switch to the <b>Archives</b>
tab.</li>
<li>Cycles are listed on the left. Clicking a cycle expands it to show
the individual archives (one full + any incrementals).</li>
<li>Click an archive to load its contents into the file tree on the right
&mdash; this is sourced from the archive's seekable sidecar, so it's fast
even for huge archives.</li>
<li>Drill into directories as you would in a file manager.</li>
</ol>

<h2>How to search across archives</h2>
<p>When you can't spot a file in the tree &mdash; or you're not sure which
cycle still has it &mdash; search instead of scrolling.</p>
<ol>
<li>On the <b>Archives</b> tab, click <b>&#128269; Search files&hellip;</b>
above the file tree.</li>
<li>Type part of a name. <b>Filename</b> mode matches the last path component;
<b>Full path</b> mode matches anywhere in the path. Search runs across
<i>every</i> archive in the plan at once, so you find a file without knowing
which cycle or shard holds it.</li>
<li>Results group by path. Expand a path to see every backup that holds a
copy, with its size and modified-at-backup time, so you can pick the version
with the content you want.</li>
<li>Double-click a result to jump straight to that file in the browse
tree.</li>
<li>Or restore without leaving search: select one or more results and click
the <b>Extract selected&hellip;</b> button on the search panel. A <i>version</i>
row extracts that exact copy; a <i>path</i> row extracts its newest
version.</li>
</ol>
<p><b>Extract always acts on the pane you're looking at.</b> The search
panel's own Extract button operates on your search selection; the bottom
Extract button (hidden while search is open) operates on the file-tree
selection. You always get the file you highlighted, never a stale pick from
the other view.</p>

<h2>How to restore from an archive</h2>
<ol>
<li>Browse to the archive you want to restore from (see <i>How to
browse</i> above).</li>
<li>In the file tree, select what you want back:
    <ul>
    <li><b>A single file</b> &mdash; click it.</li>
    <li><b>A whole directory</b> &mdash; click the directory (its subtree
    gets restored).</li>
    <li><b>Multiple files or directories</b> &mdash; Ctrl-click for
    individual picks, Shift-click for a range.</li>
    </ul>
</li>
<li>The bottom of the panel shows <b>"Selected: N paths"</b>. Click
<b>Extract selected&hellip;</b>.</li>
<li>In the Restore dialog, the destination defaults to
<code>~/Restored/&lt;archive-name&gt;/</code> so nothing in your live tree
gets overwritten by accident. Change it to any path you can write to.</li>
<li>Click <b>Extract</b> to start. TimeTraveller uses the seekable
sidecar to read only the bytes you asked for &mdash; single-file restores
from a 200 GB archive take seconds, not minutes. The output window
reports the extraction mode: <b>fast (sidecar-based)</b> when the sidecar
is usable, <b>na&iuml;ve (whole-archive scan)</b> as a slower-but-correct
fallback when it isn't.</li>
</ol>
<p>To restore an <i>entire</i> cycle, the command line is more
comfortable:</p>
<pre><code>timetraveller-backup --plan &lt;name&gt; --extract &lt;archive&gt;.pax.zst --into /restore/path .</code></pre>

<h2>Restoring from a drive with no plan configured</h2>
<p>You don't need the original machine or its config to get your files back.
On any box with TimeTraveller installed, click <b>Restore from
location&hellip;</b> in the toolbar and browse to where the backup lives
&mdash; a USB drive, an external disk, or a mounted NAS share. TimeTraveller
reads that location directly: the <b>Archives</b> tab fills with its cycles,
and browsing, searching, and <b>Extract selected&hellip;</b> all work exactly
as they do for a locally-configured plan.</p>
"""


_ABOUT_HTML = _CSS + """
<h2>About TimeTraveller</h2>
<p>TimeTraveller is a local Linux backup tool focused on <b>trustworthy
backups</b> and <b>fast, partial recovery</b>.</p>

<h2>Why it exists</h2>
<p>TimeTraveller started after frustration with the existing open-source
landscape:</p>
<ul>
<li><b>Timeshift</b>, the closest equivalent, had buggy edge cases that
occasionally left backups in inconsistent states and offered limited
control over what gets included.</li>
<li>Other tools couldn't do <b>partial recovery</b> &mdash; they treated
each backup as an opaque blob, so getting one file back meant extracting
the whole thing.</li>
<li>Most didn't play nicely with <b>NAS devices over NFS</b> &mdash;
they'd block indefinitely on a slow mount or silently skip remote
destinations.</li>
<li>Many couldn't even <b>see their mounts</b> correctly &mdash; backing
up <code>/</code> would either walk into NFS shares you didn't want, or
skip ones you did.</li>
<li><b>Incremental backups</b> with sane retention were either missing or
required hand-rolled scripts.</li>
<li><b>Glob patterns</b> for excludes (<code>**/.cache/</code>,
<code>**/node_modules/</code>) were rarely supported, forcing you to list
paths one by one.</li>
</ul>
<p>The underlying tools &mdash; <code>pax</code> and <code>zstd</code>
&mdash; already do all of this well. TimeTraveller is the thin
coordination layer that wires them together with cycle management, a
manifest, indexed sidecars for random-access extraction, and a GUI that
doesn't get in the way.</p>

<h2>How backups are stored</h2>
<p>Each backup is a <b>pax archive compressed with zstd</b>, written to
<code>&lt;destination&gt;/&lt;hostname&gt;/&lt;plan_name&gt;/&lt;date&gt;_&lt;kind&gt;.pax.zst</code>.
Alongside each archive sits a small <b>sidecar</b>
(<code>.idx.zst</code>) holding a sorted index of every entry's name and
byte offset &mdash; this is what makes single-file restore fast.</p>
<p>Cycles are tracked in a <code>manifest.json</code> next to the
archives, plus a local mirror under
<code>~/.local/state/timetraveller/</code> so the GUI never has to block
on the backup mount just to draw its list.</p>

<h2>Plan types in detail</h2>
<ul>
<li><b>Active plans</b> rotate: scheduled fulls + incrementals + retention
pruning (<code>max_cycles</code>, <code>max_age_days</code>, or
<code>max_size_gb</code>).</li>
<li><b>Archive plans</b> don't rotate: manual runs only,
<code>keep_all</code> retention. Best for write-once-read-rarely
data.</li>
</ul>

<h2>Schedules</h2>
<p>Active plans run <b>weekly</b> (e.g. full every Sunday + incrementals
Mon&ndash;Sat) or <b>monthly</b> (e.g. full on the 1st + incrementals
every 3 days). The schedule renders into your user crontab inside a
managed marker block. <b>Suspend</b> comments the entries so they don't
fire without uninstalling, <b>Resume</b> turns them back on,
<b>Uninstall</b> removes the block entirely.</p>

<h2>The command-line tool</h2>
<p>The GUI is a wrapper around <code>timetraveller-backup</code>.
Anything you can do here works from the shell &mdash; handy for scripted
restores, remote machines, or a faster feedback loop. See
<code>timetraveller-backup --help</code> for the full list. Common
one-liners:</p>
<ul>
<li><code>timetraveller-backup --plan home --kind full</code> &mdash; take
a manual full</li>
<li><code>timetraveller-backup --plan home --list-archives</code> &mdash;
list cycles</li>
<li><code>timetraveller-backup --plan home --prune</code> &mdash; apply
retention now</li>
<li><code>timetraveller-backup --plan home --extract &lt;archive&gt;.pax.zst ./path/within</code>
&mdash; restore a single path</li>
</ul>

<h2>Troubleshooting</h2>
<ul>
<li><b>"Plan doesn't appear in the sidebar"</b> &mdash; check that the
YAML in <code>~/.config/timetraveller/</code> parses cleanly. The GUI
status bar shows which files were skipped on startup.</li>
<li><b>"Schedule won't install"</b> &mdash; check <code>crontab -l</code>
for a managed block. Plan names must match <code>[A-Za-z0-9_-]+</code> to
install (the New Plan dialog enforces this).</li>
<li><b>"Restore says 'archive not found'"</b> &mdash; the file on the
backup mount was deleted outside TimeTraveller. The manifest still
references it. Restore from another backup, or remove the manifest entry
manually (a future release will detect and surface this in the GUI).</li>
</ul>
"""


class HelpDialog(QDialog):
    """Help / About dialog with two tabs and a find bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TimeTraveller Help")
        self.resize(820, 660)

        layout = QVBoxLayout(self)

        # Find bar at the top.
        find_row = QHBoxLayout()
        find_row.addWidget(QLabel("Find:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("Type to search (Enter for next)…")
        self._search.setClearButtonEnabled(True)
        find_row.addWidget(self._search, 1)
        self._prev_btn = QPushButton("◀ Prev")
        self._next_btn = QPushButton("Next ▶")
        for b in (self._prev_btn, self._next_btn):
            b.setAutoDefault(False)
        find_row.addWidget(self._prev_btn)
        find_row.addWidget(self._next_btn)
        layout.addLayout(find_row)

        # Tabs.
        self._tabs = QTabWidget()
        self._help_browser = self._make_browser(_HELP_HTML)
        self._about_browser = self._make_browser(_ABOUT_HTML)
        self._tabs.addTab(self._help_browser, "Help")
        self._tabs.addTab(self._about_browser, "About")
        layout.addWidget(self._tabs, 1)

        # Close button.
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # Wiring.
        self._search.returnPressed.connect(self._find_next)
        self._next_btn.clicked.connect(self._find_next)
        self._prev_btn.clicked.connect(self._find_prev)

        # Ctrl-F focuses the find box; Esc clears it.
        QShortcut(QKeySequence.StandardKey.Find, self,
                  activated=self._search.setFocus)
        QShortcut(QKeySequence("Escape"), self, activated=self._clear_search)

        self._search.setFocus()

    def _make_browser(self, html: str) -> QTextBrowser:
        b = QTextBrowser()
        b.setOpenExternalLinks(False)
        b.setHtml(html)
        return b

    def _current_browser(self) -> QTextBrowser:
        return self._tabs.currentWidget()  # type: ignore[return-value]

    def _find_next(self) -> None:
        text = self._search.text()
        if not text:
            return
        browser = self._current_browser()
        if not browser.find(text):
            # Wrap to the top.
            cursor = browser.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            browser.setTextCursor(cursor)
            browser.find(text)

    def _find_prev(self) -> None:
        text = self._search.text()
        if not text:
            return
        browser = self._current_browser()
        if not browser.find(text, QTextDocument.FindFlag.FindBackward):
            cursor = browser.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            browser.setTextCursor(cursor)
            browser.find(text, QTextDocument.FindFlag.FindBackward)

    def _clear_search(self) -> None:
        self._search.clear()
        # Move cursor to top of current tab so the next Find starts fresh.
        browser = self._current_browser()
        cursor = browser.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        browser.setTextCursor(cursor)
