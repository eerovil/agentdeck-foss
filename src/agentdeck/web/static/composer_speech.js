// Dictation into the chat composer and the New work form (#2456, #2464), with
// punctuation (#2469).
//
// Where the deck has a speech service (`data-host-speech`, #3409), the 🎤 button
// records the microphone, cuts each phrase at a pause and posts it to
// /api/speech/transcribe, so the host's Whisper hears it in the language the
// host's `[speech] language` names (#3422); the words of each phrase land once
// it comes back. If the host cannot be reached it falls back to the browser's
// own speech recognition for the rest of the page, which is all a deck without a
// speech service uses, and then no audio reaches the deck. Speech only ever adds to the draft at the caret and never
// sends: a misheard instruction to a coding agent is costly, so a person reads
// it first. Listening stops on the person's own tap, on send, and when the
// page is hidden. A browser ends recognition by itself after a pause or a
// silence (Chrome on Android after every phrase); that restarts it, so a pause
// to think does not end dictation (#2464). Desktop Chrome keeps listening
// through a pause, so there a short silence is what ends a phrase (#2480).
//
// The button is hidden by CSS until this script marks the page as able to
// dictate, so a browser without the API (Firefox, some home-screen iOS modes)
// never shows a dead control. Everything is delegated off `document`, so a chat
// opened in-page through session_navigation.js needs no re-init hook.
(function () {
  'use strict';

  var LANG_KEY = 'agentdeck.dictation.lang';
  var Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  // Finnish unless this device chose otherwise; `auto` is the browser's own
  // language, not the page's `lang="en"`.
  var DEFAULT_LANG = 'fi-FI';
  var LANG_NAMES = { 'fi-FI': 'Suomi', 'en-US': 'English' };

  function savedLang() {
    var lang = '';
    try { lang = localStorage.getItem(LANG_KEY) || ''; } catch (e) { /* private mode */ }
    return lang || DEFAULT_LANG;
  }
  function saveLang(lang) {
    try { localStorage.setItem(LANG_KEY, lang); } catch (e) { /* private mode */ }
  }
  function recognizerLang() {
    var lang = savedLang();
    return lang === 'auto' ? navigator.language : lang;
  }

  // The language choice lives on /settings and is per device.
  document.addEventListener('change', function (event) {
    var select = event.target.closest && event.target.closest('[data-speech-lang-setting]');
    if (!select) return;
    saveLang(select.value);
  });
  function syncSettings(root) {
    (root || document).querySelectorAll('[data-speech-lang-setting]').forEach(function (select) {
      select.value = savedLang();
    });
  }
  syncSettings(document);
  document.addEventListener('htmx:load', function (event) {
    syncSettings((event.detail && event.detail.elt) || event.target);
  });

  // ---- recording for the host's speech service (#3409) ----------------------

  var Context = window.AudioContext || window.webkitAudioContext;
  var canRecord = !!(Context && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  var HOST_RATE = 16000;

  // The microphone, recorded until closed. A listener takes the samples through
  // `sink`; while there is none and `keep` is set they wait in `carry`, so words
  // said while voice mode's last phrase is being transcribed are kept.
  function Capture(ctx, stream) {
    var self = this;
    this.stream = stream;
    this.rate = ctx.sampleRate;
    this.sink = null;
    this.keep = false;
    this.carry = [];
    this.source = ctx.createMediaStreamSource(stream);
    this.node = ctx.createScriptProcessor(4096, 1, 1);
    this.node.onaudioprocess = function (event) {
      var samples = new Float32Array(event.inputBuffer.getChannelData(0));
      if (self.sink) self.sink(samples);
      else if (self.keep) self.carry.push(samples);
    };
    this.source.connect(this.node);
    // A processor only runs while connected onward; it writes silence.
    this.node.connect(ctx.destination);
  }

  Capture.prototype.close = function () {
    this.sink = null;
    try { this.source.disconnect(); this.node.disconnect(); } catch (e) { /* gone */ }
    this.stream.getTracks().forEach(function (track) { track.stop(); });
  };

  // One phrase as 16 kHz 16-bit mono WAV, what the host's Whisper takes.
  function wav(chunks, rate) {
    var total = 0;
    chunks.forEach(function (chunk) { total += chunk.length; });
    var count = Math.floor(total * HOST_RATE / rate);
    var buffer = new ArrayBuffer(44 + count * 2);
    var view = new DataView(buffer);
    function text(at, value) { for (var i = 0; i < value.length; i++) view.setUint8(at + i, value.charCodeAt(i)); }
    text(0, 'RIFF'); view.setUint32(4, 36 + count * 2, true); text(8, 'WAVE');
    text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, HOST_RATE, true);
    view.setUint32(28, HOST_RATE * 2, true); view.setUint16(32, 2, true);
    view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, count * 2, true);
    var flat = new Float32Array(total);
    var at = 0;
    chunks.forEach(function (chunk) { flat.set(chunk, at); at += chunk.length; });
    for (var i = 0; i < count; i++) {
      var pos = i * rate / HOST_RATE;
      var lo = Math.floor(pos);
      var hi = Math.min(lo + 1, total - 1);
      var sample = flat[lo] + (flat[hi] - flat[lo]) * (pos - lo);
      view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, sample)) * 0x7fff, true);
    }
    return new Blob([buffer], { type: 'audio/wav' });
  }

  function loudness(samples) {
    var sum = 0;
    for (var i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    return Math.sqrt(sum / samples.length);
  }

  // Whether each chunk is louder than the room (#3424). Between phrases the room is
  // the quietest chunk of the last few seconds, so a room that got louder is
  // learned. Inside a phrase nothing is learned, or a long steady sentence would
  // become the room and be cut. A pause inside a phrase is instead any of:
  // - a chunk under the room's line, as before;
  // - a chunk under half the phrase's own running level;
  // - a steady stretch (STEADY_MS within ±25 %) under 80 % of that level: speech
  //   rises and falls with each syllable, a fan or traffic does not.
  // When a phrase ends on a pause, the pause's level becomes the room, so the noise
  // that ended it does not start the next phrase. With the room learned only before
  // speech, noise that rose after the words (a phone's gain turning up in the quiet)
  // kept a phrase open to its cap, so nothing was sent.
  var NOISE_WINDOW_MS = 3000;
  var STEADY_MS = 1000;
  var PAUSE_TAIL = 8;
  function Gate(chunkMs) {
    this.floor = 0.005;
    this.size = Math.max(1, Math.round(NOISE_WINDOW_MS / chunkMs));
    this.steady = Math.max(2, Math.round(STEADY_MS / chunkMs));
    this.recent = [];
    this.tail = [];
    this.speech = 0;
  }
  Gate.prototype.loud = function (level, inPhrase) {
    var line;
    if (inPhrase) {
      this.tail.push(level);
      if (this.tail.length > this.steady) this.tail.shift();
      line = Math.max(this.line(), this.speech * 0.5);
      if (level <= line || this.flat()) return false;
      // Only the speaker's own level moves it: noise a little under it must not
      // drag it down to the noise.
      if (level >= this.speech * 0.8) this.speech = this.speech * 0.9 + level * 0.1;
      return true;
    }
    this.recent.push(level);
    if (this.recent.length > this.size) this.recent.shift();
    var quietest = Math.min.apply(null, this.recent);
    // Until a whole window is in, the room only gets quieter.
    this.floor = this.recent.length === this.size ? quietest : Math.min(this.floor, quietest);
    line = this.line();
    // The chunk that starts a phrase is its first measure of the speaker.
    this.speech = level > line ? level : 0;
    this.tail = [];
    return level > line;
  };
  // How loud a chunk must be to be more than the room: 2.5 times a quiet room, but
  // never more than 0.05 over a loud one, or a voice could not rise above it.
  Gate.prototype.line = function () {
    return Math.max(0.015, Math.min(this.floor * 2.5, this.floor + 0.05));
  };
  function held(levels) {
    var low = Math.min.apply(null, levels);
    var high = Math.max.apply(null, levels);
    var mean = (low + high) / 2;
    return high <= mean * 1.25 && low >= mean * 0.75 ? mean : 0;
  }
  // The last STEADY_MS held level within ±25 %, under 80 % of the speaker's.
  Gate.prototype.flat = function () {
    if (this.tail.length < this.steady) return false;
    var mean = held(this.tail);
    return mean > 0 && mean < this.speech * 0.8;
  };
  // A phrase ended on a pause. A steady pause was the room's noise: it is the room
  // now. One that rose and fell may have been a quiet speaker, who must still be
  // able to start the next phrase, so the room stays as it was.
  Gate.prototype.paused = function () {
    var pause = this.tail.slice(-PAUSE_TAIL);
    this.tail = [];
    if (pause.length < PAUSE_TAIL || !held(pause)) return;
    this.floor = Math.max(this.floor, Math.min.apply(null, pause));
    this.recent = pause;
  };

  // Voice mode (#3401) records for the host with the same microphone code.
  window.AgentDeckDictation = { Capture: Capture, wav: wav, loudness: loudness, Gate: Gate };

  if (!Recognition && !canRecord) return;
  // The browser's recognizer shows 🎤 everywhere; recording alone shows it where
  // the deck has a speech service.
  if (Recognition) document.documentElement.classList.add('speech-supported');
  if (canRecord) document.documentElement.classList.add('speech-recordable');

  // A phrase ends after this much quiet; one never runs past the host's limit.
  var HOST_PAUSE_MS = 800;
  var HOST_PHRASE_MAX_MS = 28000;
  // Set once the host fails: the rest of the page uses the browser's recognizer.
  // With none (Firefox), each tap tries the host again and says if it still fails.
  var hostDown = false;

  // The host's Whisper behind the browser recognizer's shape (start, stop, abort,
  // onresult, onerror, onend), so a session drives either the same way. It listens
  // until stopped; each phrase, cut at a pause, comes back as one final result, in
  // the order spoken. `stop()` still delivers the phrase being spoken and any
  // still being transcribed before `onend`; `abort()` drops them.
  function HostRecognition() {
    this.results = [];
    this.pending = Promise.resolve();
    this.lang = 'en-US';
  }

  HostRecognition.prototype.start = function () {
    var self = this;
    // Made inside the tap, so a phone lets it run.
    var ctx = this.ctx = new Context();
    navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    }).then(function (stream) {
      if (self.done) { stream.getTracks().forEach(function (t) { t.stop(); }); return; }
      self.capture = new Capture(ctx, stream);
      self.listen();
    }, function () {
      if (!self.done) self.fail('not-allowed');
    });
  };

  HostRecognition.prototype.listen = function () {
    var self = this;
    var cap = this.capture;
    var chunkMs = 4096 * 1000 / cap.rate;
    var before = [];
    var phrase = [];
    var quiet = 0;
    var gate = new Gate(chunkMs);
    cap.sink = function (samples) {
      var loud = gate.loud(loudness(samples), phrase.length > 0);
      if (!phrase.length) {
        before.push(samples);
        if (before.length > 4) before.shift();
        // Any sound starts a phrase: the host's VAD, not this level check, tells a
        // word from a click or a cough.
        if (loud) { phrase = before; before = []; quiet = 0; }
        return;
      }
      phrase.push(samples);
      quiet = loud ? 0 : quiet + chunkMs;
      if (quiet >= HOST_PAUSE_MS || phrase.length * chunkMs >= HOST_PHRASE_MAX_MS) {
        if (quiet >= HOST_PAUSE_MS) gate.paused();
        self.send(phrase);
        phrase = [];
      }
    };
    this.cut = function () {
      if (phrase.length) self.send(phrase);
      phrase = [];
    };
  };

  // Phrases are transcribed one at a time, so their words land in the order spoken.
  HostRecognition.prototype.send = function (phrase) {
    var self = this;
    var body = wav(phrase, this.capture.rate);
    this.pending = this.pending.then(function () {
      if (self.aborted || self.failed) return null;
      // The language heard last, for a phrase too short to tell (`[speech] language
      // = "auto"`); a host listening for one language ignores it.
      var hint = self.heard ? '?language=' + self.heard : '';
      return fetch('/api/speech/transcribe' + hint, {
        method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: body
      }).then(function (response) {
        if (!response.ok) throw new Error('speech ' + response.status);
        return response.json();
      }).then(function (heard) {
        var text = (heard.text || '').trim();
        if (self.aborted || !text) return;
        self.heard = heard.language === 'fi' ? 'fi' : 'en';
        self.lang = self.heard === 'fi' ? 'fi-FI' : 'en-US';
        var result = [{ transcript: text }];
        result.isFinal = true;
        self.results.push(result);
        if (self.onresult) self.onresult({ resultIndex: self.results.length - 1, results: self.results });
      });
    }).catch(function () {
      if (!self.aborted) self.fail('host-failed');
    });
  };

  HostRecognition.prototype.release = function () {
    this.done = true;
    if (this.capture) this.capture.close();
    this.capture = null;
    if (this.ctx && this.ctx.close && this.ctx.state !== 'closed') this.ctx.close();
  };

  HostRecognition.prototype.fail = function (error) {
    if (this.failed) return;
    this.failed = true;
    this.release();
    if (this.onerror) this.onerror({ error: error });
    this.end();
  };

  HostRecognition.prototype.end = function () {
    var self = this;
    if (this.ended) return;
    this.ended = true;
    setTimeout(function () { if (self.onend) self.onend(); }, 0);
  };

  HostRecognition.prototype.stop = function () {
    var self = this;
    if (this.cut) this.cut();
    this.release();
    this.pending.then(function () { self.end(); });
  };

  HostRecognition.prototype.abort = function () {
    this.aborted = true;
    this.release();
    this.end();
  };

  var ERRORS = {
    'not-allowed': 'Microphone blocked',
    'service-not-allowed': 'Dictation not allowed here',
    'audio-capture': 'No microphone found',
    'network': 'Speech service unreachable',
    'language-not-supported': 'Language not supported',
    'host-failed': 'Speech service unreachable'
  };

  // One recognizer at a time, bound to the button that started it.
  var active = null;
  // The host session a tap stopped, still transcribing its last phrase (#3422).
  var draining = null;

  // A recognizer that keeps ending this soon after starting, having heard
  // nothing, is refused rather than paused; restarting it would loop forever.
  var QUICK_END_MS = 1000;
  var QUICK_ENDS_ALLOWED = 2;

  // Silence this long after a final result is a pause: the phrase gets its full
  // stop. Desktop Chrome never ends recognition at a pause, so without this its
  // dictation was one sentence until the person tapped stop (#2480). The
  // requester tried 0.6 s, 1 s and 1.5 s and chose 0.6 s.
  var PAUSE_MS = 600;

  function clearPause(session) {
    clearTimeout(session.pauseTimer);
    session.pauseTimer = null;
  }

  function statusOf(button) {
    var control = button.closest('.speech-control');
    return control && control.querySelector('.speech-status');
  }

  function showStatus(button, text, lasting) {
    var status = statusOf(button);
    if (!status) return;
    clearTimeout(status._hideTimer);
    status.textContent = text;
    status.hidden = !text;
    status.classList.remove('is-listening');
    if (text && !lasting) {
      status._hideTimer = setTimeout(function () { status.hidden = true; }, 4000);
    }
  }

  // While listening the bubble names the language, and a tap on it switches
  // Suomi <-> English and listens again in the new one (#2456 follow-up).
  // The host listens in its own `[speech] language`: there is nothing to switch.
  // A `note` says why the browser is listening instead.
  function showListening(button, host, note) {
    var status = statusOf(button);
    if (!status) return;
    if (host) {
      showStatus(button, 'Listening', true);
      status.classList.add('is-listening');
      return;
    }
    showStatus(button, '', true);
    var lang = recognizerLang();
    var name = LANG_NAMES[lang] || lang;
    var cycle = document.createElement('button');
    cycle.type = 'button';
    cycle.setAttribute('data-speech-lang-cycle', '');
    cycle.title = 'Switch dictation language';
    cycle.textContent = (note || 'Listening') + ' · ' + name;
    status.appendChild(cycle);
    status.classList.add('is-listening');
    status.hidden = false;
  }

  function setPressed(button, on) {
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    button.title = on ? 'Stop dictation' : 'Dictate';
  }

  // The text around the spoken words is fixed when listening starts (or when
  // the person types mid-dictation), and each result rewrites only the part in
  // between. Rebuilding from every result each time, rather than appending the
  // newest one, keeps a browser that re-reports earlier phrases from doubling them.
  function anchor(session) {
    var input = session.input;
    var focused = document.activeElement === input;
    var start = focused ? input.selectionStart : input.value.length;
    var end = focused ? input.selectionEnd : input.value.length;
    session.before = input.value.slice(0, start);
    session.after = input.value.slice(end);
    session.skip = session.seen;
    session.written = input.value;
    session.carried = [];
    session.phrases = [];
    session.closed = [];
    session.wordCount = 0;
  }

  function write(session, spoken) {
    var input = session.input;
    spoken = spoken.replace(/^\s+/, '');
    var sep = spoken && session.before && !/\s$/.test(session.before) ? ' ' : '';
    var tail = spoken && session.after && !/^\s/.test(session.after) ? ' ' : '';
    var value = session.before + sep + spoken + tail + session.after;
    if (value === input.value) return;
    input.value = value;
    session.written = value;
    var caret = (session.before + sep + spoken).length;
    if (document.activeElement === input) input.setSelectionRange(caret, caret);
    // Draft persistence and the autocomplete both listen for `input`; a
    // programmatic value assignment fires nothing on its own.
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // Desktop Chrome reports each phrase once ("fix the", " login bug"), but
  // Chrome on Android reports every growing version of the same sentence as its
  // own result ("okay", "okay it", "okay it works"), and starts a new chain
  // after a pause. A result that grows the previous phrase replaces it rather
  // than being added after it. Words are compared, not characters: the final
  // version often adds punctuation ("okay, it works.") or corrects the last
  // word heard ("okay if" -> "okay it works"), and either used to land the
  // whole sentence twice (#2464), and so did a word corrected mid-sentence
  // (#2472). Whole words also keep "go" then "good morning" as two phrases.
  function words(text) {
    return text.toLowerCase().replace(/[^\p{L}\p{N}\s]+/gu, ' ').trim().split(/\s+/)
      .filter(Boolean);
  }

  // Two guesses at the same spoken word: one contains the other ("kuin",
  // "kuinka"; "kaikkiin", "kaikki"), or they differ by a single letter. A
  // different word ("apples", "oranges") is not a re-guess.
  function reheard(x, y) {
    if (x.length < 3 || y.length < 3) return false;
    if (x.indexOf(y) === 0 || y.indexOf(x) === 0) return true;
    if (Math.abs(x.length - y.length) > 1) return false;
    var i = 0;
    while (i < x.length && x[i] === y[i]) i++;
    if (x.length === y.length) return x.slice(i + 1) === y.slice(i + 1);
    return x.length > y.length ? x.slice(i + 1) === y.slice(i) : x.slice(i) === y.slice(i + 1);
  }

  // 'grows' when `next` is the same utterance heard further, 'repeat' when it
  // is a shorter re-report of it, and '' when it is a new phrase.
  function relation(last, next) {
    var a = words(last);
    var b = words(next);
    if (!a.length || !b.length) return '';
    var same = 0;
    while (same < a.length && same < b.length && a[same] === b[same]) same++;
    if (same === a.length) return 'grows';
    if (same === b.length) return 'repeat';
    // Only the last word heard may change; it is the one still being guessed.
    if (a.length >= 2 && same === a.length - 1 && b.length >= a.length) return 'grows';
    // Chrome on Android re-reports a finished sentence with a word re-heard
    // anywhere in it ("kuin monta" -> "kuinka monta"), not only the last one
    // (#2472). It is the same sentence when every other word is unchanged in
    // place and at most one word in four was re-heard as a near spelling of
    // itself. A new sentence that says a different word ("I like apples
    // today", "I like oranges today") is kept.
    if (a.length < 2 || !same || b.length < a.length) return '';
    var reguessed = 0;
    for (var i = same; i < a.length; i++) {
      if (a[i] === b[i]) continue;
      if (!reheard(a[i], b[i])) return '';
      reguessed++;
    }
    return reguessed <= Math.max(1, Math.floor(a.length / 4)) ? 'grows' : '';
  }

  // The host reports each phrase once, never a re-guess of the last one.
  function merge(phrases, next, host) {
    var kind = phrases.length && !host ? relation(phrases[phrases.length - 1], next) : '';
    if (kind === 'repeat') return;
    if (kind === 'grows') {
      phrases[phrases.length - 1] = (phrases.length > 1 && !/^\s/.test(next) ? ' ' : '') + next;
    } else {
      phrases.push(phrases.length && !/^\s/.test(next) ? ' ' + next : next);
    }
  }

  // Chrome on Android returns Finnish with no punctuation at all. The browser's
  // own `unspokenPunctuation` is asked for, but until it ships a phrase ends in
  // a full stop, unless it already ends in a mark, once the browser pauses
  // (ends recognition) or the person stops, and the next one starts with a
  // capital (#2469). Another result in the same run is not a pause: desktop
  // Chrome splits one sentence into several ("fix the", " login bug"), and
  // only silence after them is (#2480). A closed
  // phrase that a later run re-reports grown is open again. This runs on the
  // text written, never on `session.phrases`, so the merge above is unchanged.
  var END_MARK = /[.?!,:;\u2026]$/;
  var SENTENCE_END = /(^|[.?!\u2026]|\n)\s*$/;

  function punctuate(session) {
    var text = '';
    var phrases = session.phrases;
    var closed = session.closed;
    var last = phrases.length - 1;
    for (var i = 0; i <= last; i++) {
      var phrase = phrases[i].trim();
      if (!phrase) continue;
      // A full stop right before words the person already wrote would split
      // their sentence.
      var ends = phrases[i] === closed[i] && (i < last || !/\S/.test(session.after));
      if (ends && !END_MARK.test(phrase)) phrase += '.';
      if (SENTENCE_END.test(text ? text : session.before)) {
        phrase = phrase.charAt(0).toLocaleUpperCase(session.rec.lang) + phrase.slice(1);
      }
      text += (text ? ' ' : '') + phrase;
    }
    return text;
  }

  // The browser paused, or the person stopped: the newest phrase is finished.
  // Earlier phrases keep whatever an earlier pause gave them.
  function close(session) {
    var last = session.phrases.length - 1;
    if (last >= 0) session.closed[last] = session.phrases[last];
    if (session.input.isConnected) write(session, punctuate(session));
  }

  function end(session) {
    clearPause(session);
    if (active !== session) return;
    active = null;
    setPressed(session.button, false);
  }

  function dropDraining() {
    var session = draining;
    draining = null;
    if (!session) return;
    showStatus(session.button, '');
    try { session.rec.abort(); } catch (e) { /* already ended */ }
  }

  function start(button, note) {
    var form = button.closest('form');
    var input = form && form.querySelector('textarea[name="message"]');
    if (!input) return;
    dropDraining();
    var control = button.closest('.speech-control');
    var host = canRecord && (!hostDown || !Recognition) && !!control &&
      control.hasAttribute('data-host-speech');
    if (!host && !Recognition) return;
    var rec = host ? new HostRecognition() : new Recognition();
    var session = { rec: rec, host: host, button: button, input: input, form: form, seen: 0, skip: 0 };
    if (!host) {
      // Auto is the browser's language, not the page's `lang="en"`.
      rec.lang = recognizerLang();
      rec.continuous = true;
      rec.interimResults = true;
      if ('unspokenPunctuation' in rec) rec.unspokenPunctuation = true;
    }
    anchor(session);

    rec.onresult = function (event) {
      if (active !== session && draining !== session) return;
      if (!input.isConnected) { stop(session, true); return; }
      session.quickEnds = 0;
      session.seen = event.results.length;
      var phrases = session.carried.slice();
      for (var i = session.skip; i < event.results.length; i++) {
        merge(phrases, event.results[i][0].transcript, host);
      }
      session.phrases = phrases;
      write(session, punctuate(session));
      // A host phrase was cut at a pause, so it is finished as it lands; waiting
      // for quiet would let the next one, landing sooner, run on from it. So is
      // the last phrase of a stopped session.
      if (host || draining === session) { close(session); return; }
      // Silence counts from the last new word: Chrome re-guessing a word it
      // already heard ("login bag" -> "login bug") is not speech, and restarting
      // the count on it made the full stop late or never come.
      var count = words(phrases.join(' ')).length;
      if (count > session.wordCount) clearPause(session);
      session.wordCount = count;
      if (event.results[event.results.length - 1].isFinal && !session.pauseTimer) {
        session.pauseTimer = setTimeout(function () {
          session.pauseTimer = null;
          if (active === session && session.input.isConnected) close(session);
        }, PAUSE_MS);
      }
    };
    rec.onerror = function (event) {
      // Silence is a pause, not a failure: `end` follows and restarts.
      if (event.error === 'aborted' || event.error === 'no-speech') return;
      // The host is gone: the browser listens instead, from `end`.
      if (event.error === 'host-failed') {
        hostDown = true;
        if (Recognition && active === session) { session.fallback = true; return; }
      }
      session.failed = true;
      showStatus(button, ERRORS[event.error] || 'Dictation stopped: ' + event.error);
    };
    // A browser always follows an error with `end`; clearing the status there
    // would wipe the reason before anyone could read it.
    rec.onend = function () {
      if (draining === session) {
        draining = null;
        if (!session.failed) showStatus(button, '');
        return;
      }
      if (session.fallback && active === session) {
        close(session);
        active = null;
        start(button, 'Host unreachable · listening');
        return;
      }
      if (session.restart) {
        clearPause(session);
        close(session);
        if (active === session) active = null;
        start(button);
        return;
      }
      if (active === session && !session.failed && restart(session)) return;
      if (active === session && !session.failed) showStatus(button, '');
      end(session);
    };

    active = session;
    session.quickEnds = 0;
    setPressed(button, true);
    showListening(button, host, note);
    if (!begin(session)) {
      end(session);
      showStatus(button, 'Could not start dictation');
    }
  }

  function begin(session) {
    session.startedAt = Date.now();
    try { session.rec.start(); return true; } catch (e) { return false; }
  }

  // The browser ended recognition while the person is still dictating. A new
  // run numbers its results from zero, so what was heard so far is carried
  // into it as its opening phrases: a new run that re-reports the last
  // sentence (Chrome on Android does) replaces it instead of adding it again.
  function restart(session) {
    if (!session.input.isConnected) return false;
    clearPause(session);
    close(session);
    if (Date.now() - session.startedAt < QUICK_END_MS) {
      session.quickEnds += 1;
      if (session.quickEnds > QUICK_ENDS_ALLOWED) {
        session.failed = true;
        showStatus(session.button, 'Dictation stopped');
        return false;
      }
    }
    session.carried = session.phrases;
    session.seen = 0;
    session.skip = 0;
    return begin(session);
  }

  // `abort` drops anything still being heard, so a late result cannot refill a
  // composer the send just cleared; the button's own stop keeps the last phrase,
  // which the host may still be transcribing.
  function stop(session, abort) {
    if (!session) return;
    session.restart = false;
    clearPause(session);
    var wasActive = active === session;
    if (!abort && wasActive) close(session);
    end(session);
    if (!abort && wasActive && session.host) {
      draining = session;
      showStatus(session.button, 'Transcribing…', true);
    } else {
      showStatus(session.button, '');
    }
    try { abort ? session.rec.abort() : session.rec.stop(); } catch (e) { /* already ended */ }
  }

  // Typing while listening moves the anchor to where the person is typing, so
  // the next spoken words land there instead of overwriting what they wrote.
  document.addEventListener('input', function (event) {
    [active, draining].forEach(function (session) {
      if (!session || event.target !== session.input) return;
      if (session.input.value !== session.written) anchor(session);
    });
  }, true);

  document.addEventListener('click', function (event) {
    var cycle = event.target.closest && event.target.closest('[data-speech-lang-cycle]');
    if (cycle) {
      event.preventDefault();
      saveLang(recognizerLang() === 'fi-FI' ? 'en-US' : 'fi-FI');
      var control = cycle.closest('.speech-control');
      var owner = control && control.querySelector('[data-speech-button]');
      if (active && active.button === owner) {
        // Stop keeps what was already heard; `end` then listens again.
        active.restart = true;
        try { active.rec.stop(); } catch (e) { active.restart = false; }
      } else if (owner) {
        showListening(owner);
      }
      return;
    }
    var button = event.target.closest && event.target.closest('[data-speech-button]');
    if (!button) return;
    event.preventDefault();
    if (active && active.button === button) { stop(active, false); return; }
    if (active) stop(active, true);
    start(button);
  });

  // Keep the mobile keyboard where it is: the button must not take focus off
  // the textarea, just like the composer's Send button.
  document.addEventListener('mousedown', function (event) {
    if (event.target.closest && event.target.closest('[data-speech-button], [data-speech-lang-cycle]')) {
      event.preventDefault();
    }
  });

  document.addEventListener('submit', function (event) {
    if (active && active.form === event.target) stop(active, true);
    if (draining && draining.form === event.target) dropDraining();
  }, true);

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) return;
    if (active) stop(active, true);
    dropDraining();
  });
})();
