(function () {
  'use strict';

  // Notes a chat shows and its agent never sees (#3227): a comment the deck's own
  // GitHub account posted on the chat's issue or pull request. The server keeps the
  // chat's notes in the hidden #chat-notes block (rendered with the page, then
  // replaced over the stream when one lands or changes), and this puts a copy of
  // each into the transcript at its time.
  //
  // It runs on the DOM for the same reason tool_call_groups.js does: transcript rows
  // arrive one stream fragment at a time and "load earlier" prepends history, so a
  // note's place is re-derived from what is actually on screen. A note older than
  // the first row on screen goes first once the chat's beginning is loaded, and
  // waits for "load earlier" while it is not.
  var activeCleanup = null;

  function timeOf(node) {
    var time = node.querySelector && node.querySelector('time.ev-time[datetime]');
    var when = time ? Date.parse(time.getAttribute('datetime')) : NaN;
    return isNaN(when) ? null : when;
  }

  function place(source, transcript) {
    var notes = Array.prototype.slice.call(source.querySelectorAll(':scope > .chat-note'));
    var wanted = {};
    notes.forEach(function (note) { wanted[note.dataset.noteId] = note; });
    // A copy whose note was deleted or edited since goes; an edit is placed again.
    Array.prototype.slice.call(transcript.querySelectorAll(':scope > .chat-note'))
      .forEach(function (copy) {
        var note = wanted[copy.dataset.noteId];
        if (!note || note.dataset.noteVersion !== copy.dataset.noteVersion) copy.remove();
      });
    notes.forEach(function (note) {
      var id = note.dataset.noteId;
      if (transcript.querySelector(':scope > .chat-note[data-note-id="' + id + '"]')) return;
      var when = Date.parse(note.dataset.noteAt);
      if (isNaN(when)) return;
      var rows = Array.prototype.slice.call(transcript.children).filter(function (row) {
        return timeOf(row) !== null;
      });
      // Older than every row on screen: its place is in history still to load, unless
      // the chat's beginning is already here (no "load earlier" button left).
      if ((!rows.length || when < timeOf(rows[0])) &&
          transcript.querySelector(':scope > .load-earlier')) return;
      var after = null;
      for (var i = 0; i < rows.length; i++) {
        if (timeOf(rows[i]) > when) break;
        after = rows[i];
      }
      var copy = note.cloneNode(true);
      copy.classList.add('ev');
      copy.id = 'chat-note-' + id;
      transcript.insertBefore(copy, after ? after.nextSibling : (rows[0] || null));
    });
  }

  function init() {
    if (activeCleanup) activeCleanup();
    activeCleanup = null;
    var source = document.getElementById('chat-notes');
    var transcript = document.querySelector('.transcript[data-session-key]');
    if (!source || !transcript) return;
    // childList only: a placed copy changes the transcript's children once, and the
    // pass it wakes finds every note already in place.
    var observer = new MutationObserver(function () { place(source, transcript); });
    observer.observe(transcript, { childList: true });
    observer.observe(source, { childList: true });
    activeCleanup = function () { observer.disconnect(); };
    place(source, transcript);
  }

  window.AgentDeckChatNotes = { init: init, place: place };
  document.addEventListener('agentdeck:session-navigation', init);
  init();
})();
