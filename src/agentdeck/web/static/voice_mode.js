// Hands-free voice mode for a chat (#3401).
//
// Two buttons, each on and off by a tap (#3540): 🎧 reads the agent's replies
// aloud, and the chat composer's 🎤 listens. Either works alone; voice mode is on
// while one of them is, and ends with both off. A tap on 🎧 while something is
// being read only stops that reading, and 🎧 stays on (#3779). Only how 🎤 listens is kept on the
// device. Which of the two were on is kept for the tab while voice mode is on, so
// the next chat opened starts with them on again (#3548).
//
// It needs the deck's speech service (`data-host-speech`, #3409), and listens and
// speaks only through it: the page records the microphone, cuts each phrase at a
// pause and posts it to /api/speech/transcribe, which hears English and Finnish, and
// plays what /api/speech/say returns. That works in any browser that can record.
// The browser's own speech is never used (#3470): when the service cannot be
// reached, after a few retries, voice mode says so and turns off.
//
// - Speaking Finnish: the next message asks the agent to answer in Finnish
//   (`data-voice-line-fi`). The words decide, not only the host's guess at the
//   sound: English words the host took for Finnish ask for no Finnish (#3417).
// - An owner who always speaks Finnish (`data-voice-language="fi"`, the
//   `[speech] language = "fi"` setting): every message asks for a Finnish answer
//   and the Finnish voice reads everything, English words and all (#3525), except a
//   question box written in English, read with the English voice (#3572).
// - Each spoken paragraph is read with the voice of the language it is written
//   in, whatever was heard since; words that do not tell use the language the
//   message asked for (#3417).
//
// - The agent decides what is spoken. Every message sent while voice mode is on
//   carries one instruction line (rendered into `data-voice-line` from
//   `agentdeck.voice_mode.VOICE_LINE`, which the transcript hides again) asking it
//   to open its reply with a paragraph starting "🔊". When the turn ends, only that
//   paragraph is read. A reply without one is read as its first prose paragraph,
//   cleaned of URLs and paths and cut short, then "More on screen."
// - Before long work the same line asks for a short note starting "🔊" first. A
//   reply that opens with one while the agent still works is read at once, once
//   per turn: any later one waits for the turn's end like any reply (#3407).
//   It does not open a microphone that already gave up listening: that opens on
//   its own only after the turn's reply (#3456).
// - A message sent while the chat holds something not read aloud — a reply that
//   landed while the person spoke, or a turn this page did not start (a wake, say) —
//   also carries `data-voice-unheard` (`agentdeck.voice_mode.UNHEARD_LINE`) naming
//   the last 🔊 line read aloud, so the agent does not take a "yes" as the answer to
//   a question the person never heard (#3520). What was unheard is taken when the
//   phrase ends, before the fast agent's answer, and no reply or note is read until
//   the words are sent, so one that lands meanwhile still counts; a reply or note
//   stopped before its end, by talking over it, a tap or anything else, counts as
//   not heard; a turn already running when voice mode turns on counts as unheard. A
//   message queued behind a turn this page did not start carries it too, a false
//   alarm accepted for safety. A finished turn's reply still unread when such a
//   message goes is then not read at all (#3771): the line asks the agent to say
//   again what the person needs of it, so it would otherwise be heard twice. A
//   spoken message that is only a yes or a no also
//   carries `data-voice-short`: a phantom "Yeah." from noise once nearly paused a
//   running batch, so the agent confirms before acting on one.
// - A fast agent talks for the chat (#3444). What was heard goes to it first
//   (`/api/voice/heard`, a Haiku process per chat in `agentdeck.voice_helper`): it
//   answers at once in the chat's voice ("Starting on the login bug.") and gives
//   back the words with speech-recognition mistakes fixed, which the composer then
//   sends. Plain noise it drops. Its memory is only what was said by voice, so the
//   page tells it each 🔊 line it read aloud since. With no answer in time the
//   words are sent as heard.
// - Words Whisper doubted reach the fast agent and the chat as "[unsure: …]"
//   (#3817), so the agent can ask what was meant rather than act on a guess. The
//   host names them by segment, and the page keeps where they sit in the words
//   heard (`v.doubts`, [start, end] pairs), so the same words said twice are told
//   apart. The page's own checks (the wake word, "stop", an echo of the reading)
//   read the words unmarked; only the copy that is asked about and sent is marked.
//   A doubted answer to a question box picks nothing: it is asked for again, by the
//   owner's decision on #3817. Voice mode's own commands ("stop", "cancel", "stop
//   listening") still act when doubted, by the owner's decision on its review: they
//   often score low yet are right, and an ignored real "stop" is worse.
// - The fast agent waits for the chat to speak (#3799): nothing is said at a send
//   but its tone. When the chat says nothing aloud for 5 s after a send while it
//   works, the fast agent says in one sentence what the agent is doing, from the
//   steps the transcript shows (`/api/voice/narrate`); with nothing from it, its
//   own line for the phrase or "Got it, working on it." (Finnish when the message
//   asked for Finnish). After that, every 30 s with nothing said while the agent
//   works brings one more such line, or "Still working on it." when it sees
//   nothing new. A question box being read counts as the chat speaking. The voice
//   log is told which of its lines were really said.
// - Sooner after the person stops talking (#3765): a phrase goes to the host after
//   0.3 s of quiet, while the 0.8 s pause that ends it runs, and that result is
//   dropped if sound comes back first. The fast agent is asked as soon as the words
//   come back, not when they are sent a second later, and its answer is thrown
//   away if more words join the phrase; the next ask names it (`drop`), so the fast
//   agent forgets it. The short "Mm." that once played while it answered was
//   removed by the owner's decision on #3780. How long after the end of speech the
//   words, the send and the first voice came is saved in the voice log.
// - After the reply it listens. Two seconds of silence after speech sends what
//   was heard through the composer's own Send, so it queues, steers and is refused
//   exactly like a typed message. "cancel" alone discards it. Through the host,
//   sound starting again holds the send back, but a cough or a fan does not hold it
//   for good: sound that brings back no words lets it go once it was due (a second
//   after the last words), and sound still going on after 2 s is checked for words
//   then (#3449).
// - While the agent works it listens too: "stop" alone presses Stop, and anything
//   else is sent as a follow-up.
// - Any listening with no words for 15 s (`[speech] idle_listen_seconds`, #3424)
//   gives up with a low tone. Through the host it then waits for "Hey Jarvis"
//   (#3425, #3476) until voice mode is turned off: each phrase still goes to the
//   host, but only one with "Hey Jarvis" in it is acted on. The words after it are
//   sent as above; "Hey Jarvis" alone beeps and listens (#3781). Who hears the wake
//   word is `data-voice-wake` (`[speech] wake`): 'detector' asks the host's wake
//   word detector first and has Whisper transcribe only a phrase it woke on;
//   'shadow' lets Whisper decide as before and only logs the detector's answer
//   beside it, so the two can be compared; 'whisper' is Whisper alone. Nothing heard while waiting
//   is written into the draft. The page has to stay open and on screen, as for any
//   listening. On a question box it rests instead: replies are still read, and a
//   tap on 🎤 answers.
// - A question box is asked one question at a time with numbered options; "option
//   two", "one and three" or free words answer it, and the box's own Submit sends
//   it. A secret question is never read: "Answer on screen."
// - Reading with the host's voice, it listens too (#3426): words that are not the
//   reading's own echo stop it and start the person's message; "stop" alone only
//   stops it. A cough, a bump or background noise does not pause it at all
//   (#3467): sound held for 0.4 s is checked for words, and only words pause it.
// - The screen is kept awake while voice mode is on; a tap on either button resumes
//   a paused voice mode. With 🎧 off nothing is said, voice mode's own lines
//   included, and messages ask for no 🔊 line.
// - 🎤 starts in Jarvis (#3787, the owner's version F of the live prototype):
//   only phrases with "Hey Jarvis" count, talking over a reading does not stop it,
//   and a question box needs the name or a tap. A tap is "talk now": from off
//   (🎤 turns on in Jarvis), while it waits for the name or while a reply is read,
//   it listens for one message and then waits for the name again. A tap while it
//   listens sends what was heard and goes to Jarvis. A long press opens a menu of
//   Jarvis, Always (all of the above: it listens after every reply and while the
//   agent works, waiting for the name after the idle spell) and Off. "lopeta
//   kuuntelu" said alone switches to Jarvis. The choice is carried to the next chat
//   in the tab like 🎧 and 🎤, never kept on the device: a new voice mode starts in
//   Jarvis. Plain dictation is gone from the chat composer; New work and new chat
//   forms still dictate (composer_speech.js).
// - Jumping between chats (#3816): the deck chat can move its page to another chat
//   (chat_jump.js), and voice mode carries on there. With voice mode on, the move waits
//   until the deck chat's turn has ended and its reply was read, so that is heard first
//   (`AgentDeckVoiceJump`). In any other chat the fast agent spots an ask to go back to
//   the deck chat, in any words, and the page opens it without sending the words. The
//   chat arrived in is named aloud ("Now in: Deck chat.") instead of "Voice mode on.".
// - It goes on speaking and listening with the page hidden (#3408), and the page's
//   live stream stays open for it. Where the browser will not do one of them there,
//   it pauses as before and the bubble says which; a tap reads a reply that
//   arrived meanwhile, then listens.
(function () {
  'use strict';

  var Context = window.AudioContext || window.webkitAudioContext;
  var canRecord = !!(Context && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  var shared = window.AgentDeckDictation;
  if (!shared || !canRecord) return;
  // 🎧 shows only where the deck has a speech service and the browser can record.
  document.documentElement.classList.add('voice-recordable');

  // Words waiting to be sent, or still being said, hold back a reply or note this long.
  var SEND_AFTER_MS = 2000;
  // Listening with no words heard for this long rests (#3424); the page's
  // `data-voice-idle-seconds` (`[speech] idle_listen_seconds`) overrides it.
  var IDLE_LISTEN_MS = 15000;
  // The final reply can land a moment after the status leaves "thinking".
  var REPLY_SETTLE_MS = 1200;
  var REPLY_WAIT_MAX_MS = 4000;
  var SPOKEN_MAX_CHARS = 300;
  var QUICK_END_MS = 1000;
  var QUICK_ENDS_ALLOWED = 3;
  var MARK = '🔊';  // 🔊
  var ACK = { en: 'Got it, working on it.', fi: 'Selvä, työn alla.' };
  // A long quiet spell where the fast agent sees nothing new to say (#3799): the work
  // is still going, and that much is true.
  var STILL = { en: 'Still working on it.', fi: 'Yhä työn alla.' };
  // What voice mode itself says, in Finnish for an owner who speaks only that (#3525).
  var SAID = {
    en: {
      on: 'Voice mode on.', onScreen: 'Answer on screen.',
      secret: 'This question needs a secret.', number: 'Please say an option number.',
      option: 'Option', more: 'More on screen.', reply: 'The reply is on screen.',
      room: 'OK, only after Hey Jarvis.', now: 'Now in:',
      again: 'I did not catch that. Please say it again.'
    },
    fi: {
      on: 'Äänitila päällä.', onScreen: 'Vastaa ruudulla.',
      secret: 'Tähän kysymykseen tarvitaan salaisuus.', number: 'Sano vaihtoehdon numero.',
      option: 'Vaihtoehto', more: 'Lisää ruudulla.', reply: 'Vastaus on ruudulla.',
      room: 'Selvä, vain Hey Jarvis.', now: 'Nyt:',
      again: 'En saanut selvää. Sano uudelleen.'
    }
  };
  // The fast agent answers in about a second; past this the words go as heard.
  var FAST_WAIT_MS = 6500;
  // The chat silent this long after a send, or this long while it works, has the
  // fast agent say what it is doing (#3799); `data-voice-quiet-seconds` ("5 30")
  // overrides both.
  var NARRATE_FIRST_MS = 5000;
  var NARRATE_EVERY_MS = 30000;
  // A send whose turn has not started by then is not waited on any more.
  var NARRATE_GIVE_UP_MS = 60000;
  // The 🔊 lines read aloud that the fast agent is told with the next phrase.
  var ALOUD_KEPT = 5;
  // Host listening: a phrase ends after this much quiet, and is sent this long after
  // its words come back unless speech starts again.
  var HOST_PAUSE_MS = 800;
  var HOST_SEND_AFTER_MS = 1000;
  // The phrase goes to the host after this much quiet, inside the pause (#3765).
  var HOST_EARLY_MS = 300;
  // The first voice this long after a send is not the phrase's any more.
  var VOICE_TIMED_MS = 10000;
  var HOST_NO_SPEECH_MS = 8000;
  var HOST_PHRASE_MAX_MS = 28000;
  var HOST_STALL_MS = 3000;
  // Sound that starts while words wait to be sent holds the send back; after this
  // long the sound so far is checked, and with no words in it the send goes (#3449).
  var HOST_HOLD_CHECK_MS = 2000;
  // A crashed speech service is started again by its socket on the next request
  // (#3424): retry after these waits before voice mode turns off.
  var HOST_RETRY_MS = [1500, 3000, 6000];
  // A phrase spoken while the service loads its model (just started at login or
  // after a crash) waits for it, saying so, rather than voice mode turning off: a
  // phrase that fails while the service says it is loading is sent again after
  // this wait, this many times (#3766).
  var LOADING_MODEL = 'Loading speech model…';
  var LOADING_RETRY_MS = 2000;
  var LOADING_RETRIES = 10;
  var WAITING = 'Say “Hey Jarvis” · tap 🎤 to talk';
  // The device-kept mode of #3540, cleared on load (#3787).
  var OLD_LISTEN_KEY = 'agentdeck.voiceListenMode';
  var MIC_TITLE = {
    off: 'Tap to talk · hold for Jarvis, Always or Off',
    listening: 'Listening · tap when done · hold for the menu',
    waiting: 'Waiting for “Hey Jarvis” · tap to talk · hold for the menu'
  };
  // 🎧 and 🎤 as they were in the chat left, for the next one opened (#3548).
  var CARRY_KEY = 'agentdeck.voiceCarry';
  // The chat a jump moves to, named aloud on arriving there (#3816).
  var ARRIVE_KEY = 'agentdeck.voiceArrive';
  // A deck chat jump still waiting for its turn's reply this long goes anyway.
  var JUMP_WAIT_MS = 90000;
  // A carried voice mode whose audio the browser will not start without a tap.
  var CARRY_PAUSED = 'Voice mode paused · tap 🎧 to go on';
  var CARRY_START_MS = 1000;
  var TAP_TO_ANSWER = 'Answer on screen · tap 🎤 to answer';
  // Said alone, it switches 🎤 to Jarvis (#3540, #3787).
  var ROOM_PHRASE = /^(?:(?:lopeta|lopetta|lopettaa) kuuntel\w*|älä kuuntele|stop listening)$/;
  // How Whisper writes "Hey Jarvis" (#3476): "Hey, Jarvis.", "Hi Jarvis!", "Hei Jarvis".
  var WAKE_WORD = /\b(?:hey|hei|hay|hi|hai|a)[\s,.!-]*(?:jarvis|jarvi|javis|jarves|jarvus)\b[\s,.!?:;-]*/i;

  // Hidden, speech that makes no progress for this long, plus 0.1 s a character
  // of what it reads, has been stopped by the browser.
  var HIDDEN_SPEAK_STALL_MS = 4000;
  var STALL_MS_PER_CHAR = 100;

  // Short enough for a phone's bubble; a tap resumes, as from any pause.
  var BACKGROUND = {
    listen: 'Paused · can’t listen in background',
    speak: 'Paused · can’t speak in background',
    frozen: 'Paused · tab stopped in background'
  };

  var NUMBER_WORDS = {
    one: 1, first: 1, won: 1, two: 2, second: 2, to: 2, too: 2, three: 3, third: 3,
    four: 4, fourth: 4, for: 4, five: 5, fifth: 5, six: 6, sixth: 6, seven: 7,
    seventh: 7, eight: 8, eighth: 8, nine: 9, ninth: 9, ten: 10, tenth: 10,
    yksi: 1, ykkönen: 1, ensimmäinen: 1, kaksi: 2, kakkonen: 2, toinen: 2, kolme: 3,
    kolmonen: 3, kolmas: 3, neljä: 4, nelonen: 4, neljäs: 4, viisi: 5, vitonen: 5,
    viides: 5, kuusi: 6, seitsemän: 7, kahdeksan: 8, yhdeksän: 9, kymmenen: 10
  };

  var UNREACHABLE = 'Speech service unreachable';

  // The one voice-mode state on the page, or null when it is off.
  var voice = null;
  // The audio of a chat just left in place, playing already, for the next one.
  var spare = null;

  function sessionKey() {
    var transcript = document.querySelector('.transcript[data-session-key]');
    return transcript ? transcript.getAttribute('data-session-key') : '';
  }

  function current() {
    if (!voice) return null;
    if (!voice.form.isConnected || sessionKey() !== voice.key) {
      off('', true);
      return null;
    }
    return voice;
  }

  // ---- status bubble and button ------------------------------------------

  function showStatus(text) {
    var status = voice && voice.status;
    if (!status) return;
    setStatus(status, text);
  }

  // The bubble hangs from 🎧's right edge. On a crowded phone composer there is
  // less room to its left than the text needs, so it slides right until it is
  // back on the screen, rather than running off the left edge (#3497). Its CSS
  // max-width (70vw) keeps it narrower than the screen.
  var STATUS_MARGIN = 8;

  function setStatus(status, text) {
    status.textContent = text;
    status.hidden = !text;
    status.style.right = '';
    if (!text) return;
    var left = status.getBoundingClientRect().left;
    if (left < STATUS_MARGIN) status.style.right = (left - STATUS_MARGIN) + 'px';
  }

  function setMode(mode) {
    if (voice.mode !== mode && (voice.mode === 'waiting' || mode === 'waiting')) {
      logWaiting(voice, mode === 'waiting');
    }
    voice.mode = mode;
    voice.button.setAttribute('data-voice-state', mode);
    showMic(voice);
    announce(mode);
  }

  // 🎤 shows whether voice mode listens through it: `data-voice-hears` while it is
  // switched on, `data-voice-state` while a listener runs. Its `aria-pressed` stays
  // dictation's own, which `dictating()` reads.
  function showMic(v) {
    if (!v.mic) return;
    var on = v.hears || listeningMode(v);
    if (on) v.mic.setAttribute('data-voice-hears', '');
    else v.mic.removeAttribute('data-voice-hears');
    if (listeningMode(v)) v.mic.setAttribute('data-voice-state', v.mode);
    else v.mic.removeAttribute('data-voice-state');
    v.mic.title = !on ? MIC_TITLE.off : v.mode === 'waiting' ? MIC_TITLE.waiting : MIC_TITLE.listening;
  }

  // A name for a waiting phrase or a spell of waiting in the wake log (#3476).
  function wakeLogId() {
    return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }

  // When a spell of waiting starts and stops, for the wake log, so the report counts
  // the time really spent waiting (#3476); best effort, and only where the detector
  // is asked.
  function logWaiting(v, start) {
    if (v.wake === 'whisper') return;
    if (start) v.spell = wakeLogId();
    if (!v.spell) return;
    var body = JSON.stringify({ waiting: start ? 'start' : 'stop', spell: v.spell });
    if (!start) v.spell = null;
    try {
      fetch('/api/speech/wake-log', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: body
      }).catch(function () { /* only logged */ });
    } catch (e) { /* only logged */ }
  }

  // A page closed while waiting ends the spell too.
  window.addEventListener('pagehide', function () {
    if (voice && voice.mode === 'waiting') logWaiting(voice, false);
  });

  // Every change of state is told to the page. base.html keeps
  // the live stream open with the page hidden while `html.voice-on` is set, or no
  // reply would reach it, and closes it on 'off' (#3408).
  function announce(mode) {
    document.documentElement.classList.toggle('voice-on', mode !== 'off');
    document.dispatchEvent(new CustomEvent('agentdeck:voice-state', { detail: { mode: mode } }));
  }

  // ---- tones --------------------------------------------------------------

  function tone(freq) {
    var ctx = voice && voice.audio;
    if (!ctx) return;
    try {
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.18);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.2);
    } catch (e) { /* no audio here */ }
  }

  // ---- what to speak ------------------------------------------------------

  // Block-level pieces of a message as plain text. The markdown pass renders a
  // paragraph break as two <br>s and code, lists and tables as their own
  // elements; before it runs the text is raw markdown. Both read the same way.
  var BLOCK = /^(P|PRE|UL|OL|TABLE|BLOCKQUOTE|H[1-6]|DIV|HR|DETAILS)$/;

  // Inline code is for the screen: a node's text without its <code> spans.
  function proseText(node) {
    if (node.tagName === 'CODE') return '';
    var copy = node.cloneNode(true);
    copy.querySelectorAll('code').forEach(function (code) { code.remove(); });
    return copy.textContent;
  }

  function blocks(textEl) {
    var parts = [];
    var inline = '';
    function flush() {
      inline.split(/\n\s*\n/).forEach(function (chunk) {
        var text = chunk.trim();
        if (!text) return;
        var prose = !/^(```|~~~|\||#|[-*+] |\d+\. |>)/.test(text) && text.indexOf('```') < 0;
        parts.push({ prose: prose, text: text });
      });
      inline = '';
    }
    textEl.childNodes.forEach(function (node) {
      if (node.nodeType === 3) { inline += node.nodeValue; return; }
      if (node.nodeType !== 1) return;
      if (node.tagName === 'BR') { inline += '\n'; return; }
      if (BLOCK.test(node.tagName)) {
        flush();
        var prose = node.tagName === 'P';
        var text = (prose ? proseText(node) : node.textContent).trim();
        if (text) parts.push({ prose: prose, text: text });
        return;
      }
      inline += proseText(node);
    });
    flush();
    return parts;
  }

  // Raw markdown's code spans, URLs and file paths, absolute or relative
  // ("/var/app.py", "src/app.py", "foo/bar"), are not read; "and/or" is a word.
  var PATH = /(^|\s)[\w.~-]*(?:\/[\w.-]+)+\/?(?=[\s,;:!?)]|$)/g;

  function speakable(text) {
    return text
      .replace(/`[^`\n]*`/g, '')
      .replace(/https?:\/\/\S+/g, '')
      .replace(PATH, function (match, lead) { return /^\s?and\/or$/i.test(match) ? match : lead; })
      .replace(/[`*_#>|]/g, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  // Short words only one of the two languages uses, as whole words.
  var FI_WORDS = /^(ja|ei|se|ne|että|kun|mitä|mikä|miten|missä|onko|olen|olet|ole|tämä|tuo|nyt|myös|mutta|vain|jo|vielä|kyllä|joo|en|et|hän|minä|sinä|voi|voit|sitten|jos|niin|tai|siitä|sen|kaikki|tee|katso|kerro|valmis|selvä)$/;
  var EN_WORDS = /^(the|a|an|and|is|are|was|were|be|been|what|which|who|how|why|where|when|has|have|had|do|does|did|of|to|in|it|its|this|that|these|those|with|for|you|your|i|we|they|she|not|yes|can|will|would|should|could|there|here|from|about|please|done|ready|okay|ok|all|any|some|my|our|one|two|now|no|so|up|out|just|new|see|need)$/;
  // The shape of a word the lists do not hold. Finnish: its own letters, long
  // vowels, an "h" next to a consonant ("virhe", "tehty"), case and verb endings.
  // English: "th", "sh", "ck" and the like, letters Finnish words lack, its own
  // endings, and a final consonant Finnish words do not end in.
  var FI_SHAPE = /[åäö]|aa|ii|uu|yy|[bdfjklmnrv]h|h[bdfgjklmnprstv]|(ssa|sta|lla|lta|lle|ksi|nen|tiin|aan|iin|sti|ttu|tty|tu|ty|ut|yt|vat|mme|tte)$/;
  var EN_SHAPE = /th|sh|ch|wh|ph|gh|ck|ee|oo|ea|ou|[cqwxzfb]|(ing|ed|ly|tion|ness|ment|ful|ous)$|[bcdfghjkmpqvwxz]$/;

  // 'fi' or 'en' for what `text` is written in, '' when its words do not tell.
  function wordsLanguage(text) {
    var fi = 0;
    var en = 0;
    (text || '').toLowerCase().split(/[^a-zåäö']+/).forEach(function (word) {
      if (!word) return;
      if (FI_WORDS.test(word)) { fi++; return; }
      if (EN_WORDS.test(word)) { en++; return; }
      var finnish = FI_SHAPE.test(word);
      var english = EN_SHAPE.test(word);
      if (finnish && !english) fi++;
      else if (english && !finnish) en++;
    });
    if (fi === en) return '';
    return fi > en ? 'fi' : 'en';
  }

  function cut(text) {
    if (text.length <= SPOKEN_MAX_CHARS) return text;
    var clipped = text.slice(0, SPOKEN_MAX_CHARS);
    var stop = Math.max(clipped.lastIndexOf('. '), clipped.lastIndexOf('? '),
      clipped.lastIndexOf('! '));
    if (stop > SPOKEN_MAX_CHARS / 3) return clipped.slice(0, stop + 1);
    return clipped.slice(0, clipped.lastIndexOf(' ')) + '…';
  }

  // A message that opens with a "🔊" paragraph: its words, else ''.
  function markedOpening(textEl) {
    var first = blocks(textEl)[0];
    if (!first || !first.prose || first.text.indexOf(MARK) !== 0) return '';
    return speakable(first.text.slice(MARK.length));
  }

  // The "🔊" paragraph if the agent wrote one; else the first prose paragraph.
  function spokenPart(textEl) {
    var parts = blocks(textEl);
    for (var i = 0; i < parts.length; i++) {
      // Only a prose paragraph: a 🔊 inside a code block is still code.
      if (parts[i].prose && parts[i].text.indexOf(MARK) === 0) {
        return speakable(parts[i].text.slice(MARK.length));
      }
    }
    for (var j = 0; j < parts.length; j++) {
      if (!parts[j].prose) continue;
      var text = speakable(parts[j].text);
      if (!text) continue;
      var clipped = cut(text);
      var more = parts.length > 1 || clipped !== text;
      return clipped + (more ? ' ' + ownLine('more') : '');
    }
    return parts.length ? ownLine('reply') : '';
  }

  function lastReply() {
    var replies = document.querySelectorAll('.transcript .ev.assistant:not(.sub):not(.failed)');
    var last = replies[replies.length - 1];
    return last && last.querySelector('.ev-text') ? last : null;
  }

  // ---- speaking -----------------------------------------------------------

  // One of voice mode's own lines (`SAID`), in the language the owner speaks.
  function ownLine(key) {
    var v = current();
    return SAID[(v && v.always) || 'en'][key];
  }

  // `lang` is 'en' or 'fi'; unset, the owner's own language.
  function speak(text, then, lang) {
    var v = current();
    if (!v) return;
    // 🎧 off (#3540): voice mode's own lines go unsaid, and what follows them runs.
    if (!v.reads) {
      stopListening(v);
      if (then) then();
      return;
    }
    if (document.hidden && v.blocked.speak) { background(v, 'speak'); return; }
    stopListening(v);
    clearTimeout(v.speechWatch);
    clearTimeout(v.quietTimer);
    silence(v);
    v.noting = false;
    v.voicedAt = Date.now();
    var token = {};
    v.speaking = token;
    setMode('speaking');
    showStatus('Speaking…');
    hostSpeak(v, text, lang || v.always || 'en', token, then);
  }

  function spoke(token, then) {
    if (!voice || voice.speaking !== token) return;
    var v = voice;
    v.speaking = null;
    v.source = null;
    v.voicedAt = Date.now();
    if (then) then();
    if (voice === v && !v.speaking) watchQuiet(v);
  }

  // A request to the host's speech service. A 503 or a dropped connection is most
  // likely the service restarting after a crash, so the same request is sent again
  // a few times, saying so, before the caller is told it failed (#3424). `back`
  // puts the status back once a retried request gets through.
  function hostRequest(v, url, init, live, back) {
    var tries = 0;
    function attempt() {
      return fetch(url, init).then(function (response) {
        if (response.status >= 500) throw new Error('speech ' + response.status);
        if (!response.ok) {
          var refused = new Error('speech ' + response.status);
          refused.final = true;
          throw refused;
        }
        if (tries && voice === v && live()) back();
        return response;
      }).catch(function (error) {
        if (error.final || tries >= HOST_RETRY_MS.length || !live()) throw error;
        var wait = HOST_RETRY_MS[tries++];
        if (voice === v) showStatus('Speech service restarting · trying again…');
        // A hidden tab's speech watch counts from after the wait: this silence is ours.
        if (voice === v && v.speaking) v.progressAt = Date.now() + wait;
        return new Promise(function (resolve) { setTimeout(resolve, wait); }).then(function () {
          if (!live()) throw error;
          return attempt();
        });
      });
    }
    return attempt();
  }

  // A sentence ends at . ! ? or … with a capital, a digit or a quote after the
  // space, so "e.g. the" stays whole; a piece of under three letters ("1.") joins
  // the next one.
  var SENTENCE_END = /([.!?…]+["'”’)\]]*)\s+(?=["'“‘(]?[\p{Lu}\p{N}])/u;

  function sentences(text) {
    var pieces = [];
    var rest = text.trim();
    var carry = '';
    while (rest) {
      var found = SENTENCE_END.exec(rest);
      var piece = found ? rest.slice(0, found.index + found[1].length) : rest;
      rest = found ? rest.slice(found.index + found[0].length) : '';
      piece = carry + piece;
      carry = '';
      if (rest && piece.replace(/[^\p{L}]/gu, '').length < 3) { carry = piece + ' '; continue; }
      pieces.push(piece);
    }
    return pieces;
  }

  // The host's voice, played through the AudioContext the 🎧 tap opened, so iOS
  // lets it play later without a tap of its own. It is asked for one sentence at a
  // time (#3480): the first plays as soon as it is ready, and the rest are made in
  // turn while it plays, so the wait before speech does not grow with the text.
  function hostSpeak(v, text, lang, token, then) {
    var parts = sentences(text);
    var played = { text: text, parts: parts.length ? parts : [text], buffers: [], index: 0,
      token: token, then: then };
    // Hidden, the watch below pauses it if the audio never starts or stops moving.
    progressed(v, played.parts[0].length);
    watchSpeech(v);
    function live() { return voice === v && v.speaking === token; }
    function failed() {
      if (live()) off(UNREACHABLE);
    }
    function fetchPart(i) {
      hostRequest(v, '/api/speech/say', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: played.parts[i], language: lang })
      }, live, function () {
        showStatus('Speaking…');
      }).then(function (response) {
        return response.arrayBuffer();
      }).then(function (data) {
        if (!live()) return;
        v.audio.decodeAudioData(data, function (buffer) {
          if (!live()) return;
          played.buffers[i] = buffer;
          if (i + 1 < played.parts.length) fetchPart(i + 1);
          if (!i) {
            play(v, played, 0);
            overhear(v, v.played);
          } else if (v.played === played && played.waiting && played.index === i) {
            // The sentence before ended first: this one goes on from there.
            played.waiting = false;
            play(v, played, 0);
          }
        }, failed);
      }).catch(failed);
    }
    fetchPart(0);
  }

  // Plays the current sentence of the host's voice from `offset` seconds in,
  // remembering where it started so a pause knows where to come back to. Its end
  // plays the next sentence, or waits for it to be made; the last one's end is the
  // reading's.
  function play(v, played, offset) {
    var source = v.audio.createBufferSource();
    source.buffer = played.buffers[played.index];
    source.connect(v.audio.destination);
    source.onended = function () {
      if (v.source !== source) return;
      if (played.index + 1 < played.parts.length) {
        played.index++;
        v.source = null;
        if (played.buffers[played.index]) {
          play(v, played, 0);
        } else {
          played.waiting = true;
          progressed(v, played.parts[played.index].length);
        }
        return;
      }
      var overheard = v.overheard;
      v.played = null;
      v.overheard = null;
      if (overheard) overheard.abort();
      spoke(played.token, played.then);
    };
    played.from = v.audio.currentTime - offset;
    v.played = played;
    v.source = source;
    source.start(0, offset);
    if (!played.index && !offset) heardVoice(v);
  }

  // ---- talking over the host's voice (#3426) -------------------------------

  // The browser's echo cancelling keeps the host's voice out of the microphone (on
  // the owner's Android phone none of it reached it), so while it reads, the
  // microphone stays open and each phrase goes to the host: words that are not the
  // reading's own stop it and start the person's message; "stop" alone only stops
  // it. A cough, a bump or noise reads on untouched (#3467): sound held for
  // OVERHEAR_CHECK_MS is checked for words, and again every OVERHEAR_RECHECK_MS of
  // sound, and only words that are not the reading's echo pause it while the
  // phrase is heard out; an echo after all resumes it a second before the pause.
  // Every pause resumes, however many (the owner's decision on #3426).
  // In Jarvis mode the room's talk would cut every reply short (#3540): each phrase
  // goes to the wake word detector instead, and only "Hey Jarvis" stops the reading
  // (#3798). A reading that says the name itself is heard by Whisper, so its own
  // echo (which the detector wakes on) is let go of.
  var REWIND_S = 1;
  var OVERHEAR_CHECK_MS = 400;
  var OVERHEAR_RECHECK_MS = 700;

  function overhear(v, played) {
    if (!v.hears || !v.capture || v.played !== played) return;
    var rec = new HostRecognition(v);
    v.overheard = rec;
    if (v.room) rec.waking = WAKE_WORD.test(played.text) ? 'whisper' : v.wake;
    var checked = 0;
    var checking = false;
    rec.onsound = function (soundMs) {
      if (checking || rec.waking || v.overheard !== rec || !v.source) return;
      if (soundMs < (checked ? checked + OVERHEAR_RECHECK_MS : OVERHEAR_CHECK_MS)) return;
      checked = soundMs;
      checking = true;
      rec.probe(function (spoken, text) {
        checking = false;
        if (!spoken || echoOf(text, played.text) || v.overheard !== rec || !v.source) return;
        var source = v.source;
        v.source = null;
        played.held = true;
        played.at = Math.max(0, v.audio.currentTime - played.from);
        try { source.stop(); } catch (e) { /* already ended */ }
        progressed(v, 0);
        showStatus('Listening…');
      });
    };
    var lang = [v.lang, v.heardLang];
    rec.onresult = function (event) {
      if (v.overheard !== rec) return;
      var text = event.results[0][0].transcript;
      var name = rec.waking && WAKE_WORD.exec(text);
      if ((rec.waking && !name && !event.results[0].woke) || echoOf(text, played.text)) {
        // What the host made of the reading is not what the person speaks.
        v.lang = lang[0];
        v.heardLang = lang[1];
        return;
      }
      v.overheard = null;
      v.played = null;
      // Words too short to be checked ("stop") did not pause it.
      var source = v.source;
      v.source = null;
      if (source) { try { source.stop(); } catch (e) { /* already ended */ } }
      // Woken, the words after the name are the message, and with none it beeps and
      // listens for one (#3781); "Hey Jarvis, stop" only stops it.
      var doubts = event.results[0].doubts || [];
      if (name) {
        var after = afterWake(text, doubts, name);
        text = after.text;
        doubts = after.doubts;
      }
      var stop = words(text).join(' ') === 'stop';
      if (rec.waking && !stop) v.once = true;
      // Its end listens, and that listening starts from these words.
      if (!stop) {
        v.interrupted = text;
        v.interruptedDoubts = doubts;
      }
      var reading = v.reading;
      spoke(played.token, played.then);
      v.interrupted = '';
      v.once = false;
      cutShort(v, reading);
    };
    // A stalled microphone or a failed host: the rest of this reading is not heard.
    rec.onerror = function () { if (v.overheard === rec) played.deaf = true; };
    rec.onend = function () {
      if (v.overheard !== rec) return;
      v.overheard = null;
      if (voice !== v || v.played !== played || v.speaking !== played.token) return;
      if (played.held) {
        played.held = false;
        play(v, played, Math.max(0, played.at - REWIND_S));
        showStatus('Speaking…');
      }
      if (!played.deaf) overhear(v, played);
    };
    try { rec.start(); } catch (e) { v.overheard = null; }
  }

  // Nothing, or two or more words the reading says in that order with nothing
  // between: what the microphone heard was not the person. Shared words are not
  // enough, or "the branch is not merged" over "the branch is merged" is lost.
  function echoOf(text, reading) {
    var said = words(text);
    if (!said.length) return true;
    if (said.length < 2) return false;
    return (' ' + words(reading).join(' ') + ' ').indexOf(' ' + said.join(' ') + ' ') >= 0;
  }

  function progressed(v, chars) {
    v.sentenceChars = chars;
    v.progressAt = Date.now();
    v.stallMs = HIDDEN_SPEAK_STALL_MS + chars * STALL_MS_PER_CHAR;
  }

  // While hidden and speaking, a browser that suspends speech without a word
  // (before it starts, or part-way after a tab switch) pauses voice mode with the
  // reason. Started by `speak` and by the page hiding; it stops with either.
  function watchSpeech(v) {
    clearTimeout(v.speechWatch);
    if (!document.hidden || !v.speaking) return;
    var token = v.speaking;
    v.speechWatch = setTimeout(function () {
      if (voice !== v || v.speaking !== token) return;
      // The host's voice is moving while the audio clock moves.
      if (v.source && v.audio.currentTime !== v.audioAt) {
        v.audioAt = v.audio.currentTime;
        progressed(v, 0);
      }
      // Paused while what was said over it is transcribed (#3426): our own silence.
      // A stalled microphone or a failed host ends that listener, which plays on.
      if (!v.source && v.played && v.played.held && v.overheard) progressed(v, 0);
      if (document.hidden && Date.now() - v.progressAt > v.stallMs) background(v, 'speak');
      else watchSpeech(v);
    }, 1000);
  }

  function stopSpeaking(v) {
    clearTimeout(v.speechWatch);
    v.speaking = null;
    cutShort(v, v.reading);
    v.reading = null;
    silence(v);
  }

  // A reading that ends was heard (#3520): only then is it the last line heard, so
  // no way of stopping one early can leave a line half heard as the last one.
  function heardOut(v, text) {
    v.reading = null;
    v.cutReading = false;
    v.lastAloud = text;
  }

  // A reply or note stopped before its end — talked over, a tap, a hold, the page
  // hidden — was not heard (#3520): the next message says so, naming the last line
  // heard in full. Talked over, its end ran `heardOut`, which this undoes.
  function cutShort(v, reading) {
    if (!reading) return;
    v.cutReading = true;
    v.lastAloud = reading.last;
  }

  // Stops whatever is playing, without forgetting what was being read.
  function silence(v) {
    var source = v.source;
    var overheard = v.overheard;
    v.source = null;
    v.played = null;
    v.overheard = null;
    if (overheard) overheard.abort();
    if (source) { try { source.stop(); } catch (e) { /* already ended */ } }
  }

  // ---- listening through the host ------------------------------------------

  // The microphone, recorded the whole time voice mode is on, with 🎤's own
  // recorder (composer_speech.js, #3422). A listener takes the samples through
  // `sink`; between a phrase's cut and the next listener they wait in `carry`, so
  // words said while the last phrase is being transcribed are kept.
  var Capture = shared.Capture;
  var wav = shared.wav;
  var loudness = shared.loudness;
  var Gate = shared.Gate;

  // One phrase, with a speech recognizer's shape (start, abort, onresult, onerror,
  // onend, plus onspeechstart), which `run` drives. It
  // ends after one transcribed phrase, or after 8 s with no speech. `onsound`
  // hears how long the phrase has been loud so far, chunk by chunk.
  function HostRecognition(v) {
    this.v = v;
    this.done = false;
  }

  HostRecognition.prototype.start = function () {
    var self = this;
    var v = this.v;
    if (!v.capture) {
      if (!v.captureReady) throw new Error('no microphone');
      v.captureReady.then(function () { if (!self.done && v.capture) self.start(); });
      return;
    }
    var cap = v.capture;
    var chunkMs = 4096 * 1000 / cap.rate;
    var before = [];
    var phrase = [];
    var quiet = 0;
    var waited = 0;
    var sound = 0;
    // The room's level outlives each listener, on the microphone it was heard by.
    var gate = cap.gate || (cap.gate = new Gate(chunkMs));
    function feed(samples) {
      if (self.done) return;
      self.fedAt = Date.now();
      var loud = gate.loud(loudness(samples), phrase.length > 0);
      if (!phrase.length) {
        before.push(samples);
        if (before.length > 4) before.shift();
        waited += chunkMs;
        // Any sound starts a phrase, so a short "ei" is kept: the host's VAD, not
        // this level check, tells a word from a click or a cough.
        if (loud) {
          phrase = before.slice();
          before = [];
          sound = chunkMs;
          if (self.onspeechstart) self.onspeechstart();
          if (self.onsound) self.onsound(sound);
        } else if (waited >= HOST_NO_SPEECH_MS) {
          self.finish(null);
        }
        return;
      }
      phrase.push(samples);
      quiet = loud ? 0 : quiet + chunkMs;
      if (loud) {
        sound += chunkMs;
        // Sound came back inside the pause: the early words are not the phrase's.
        self.early = null;
        if (self.onsound) self.onsound(sound);
      }
      if (quiet >= HOST_PAUSE_MS || phrase.length * chunkMs >= HOST_PHRASE_MAX_MS) {
        if (quiet >= HOST_PAUSE_MS) gate.paused();
        self.endedAt = Date.now() - quiet;
        self.finish(phrase);
        return;
      }
      // Recognized early in the pause (#3765); only quiet joins the phrase after it.
      if (quiet >= HOST_EARLY_MS && !self.early && self.recognizesEarly()) {
        self.early = self.request(wav(phrase, cap.rate));
      }
    }
    cap.sink = feed;
    // The phrase so far, for a check that does not end it (#3449).
    self.sofar = function () { return phrase.slice(); };
    // A browser that suspends audio (a phone's background tab) stops the samples:
    // that ends this listener with an error, which hidden pauses voice mode.
    self.fedAt = Date.now();
    var watch = setInterval(function () {
      if (self.done) { clearInterval(watch); return; }
      if (Date.now() - self.fedAt < HOST_STALL_MS) return;
      clearInterval(watch);
      self.done = true;
      if (cap.sink === feed) cap.sink = null;
      if (!document.hidden && v.audio.resume) v.audio.resume();
      if (self.onerror) self.onerror({ error: 'audio-stalled' });
      self.end();
    }, 1000);
    var carried = cap.carry;
    cap.carry = [];
    cap.keep = false;
    carried.forEach(feed);
  };

  HostRecognition.prototype.finish = function (phrase) {
    var self = this;
    var cap = this.v.capture;
    this.done = true;
    if (cap.sink) cap.sink = null;
    if (!phrase) { this.end(); return; }
    // Words said while this phrase is transcribed wait for the next listener.
    cap.keep = true;
    var v = this.v;
    var wake = this.wake();
    if (wake === 'whisper' && this.early) { this.transcribe(null, null, this.early); return; }
    var body = wav(phrase, cap.rate);
    if (wake === 'whisper') { this.transcribe(body); return; }
    // Waiting (#3476): the phrase is named, so the wake log can join what the
    // detector and Whisper each made of it.
    var id = wakeLogId();
    var ended = Date.now();
    var url = '/api/speech/wake?id=' + id;
    var init = { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: body };
    if (wake === 'shadow') {
      fetch(url, init).catch(function () { /* only logged */ });
      this.transcribe(body, { id: id, ended: ended });
      return;
    }
    // Not woken, nothing more happens to the phrase; woken with nothing after it, it
    // is the wake word alone. A detector that cannot answer leaves it to Whisper.
    hostRequest(v, url, init, function () { return !self.aborted; }, function () {
      if (v.mode === 'waiting') showStatus(WAITING);
    }).then(function (response) {
      return response.json();
    }).then(function (found) {
      if (self.aborted) return;
      if (!found.woken) { self.end(); return; }
      if (!found.speech_after) { self.heard('Hey Jarvis', true); self.end(); return; }
      self.transcribe(body, { id: id, ended: ended, detected: true });
    }).catch(function () {
      if (!self.aborted) self.transcribe(body, { id: id, ended: ended });
    });
  };

  // Only listening for a message recognizes early (#3765): not a question box, the
  // wait for "Hey Jarvis", or talk heard over a reading.
  HostRecognition.prototype.recognizesEarly = function () {
    var v = this.v;
    return v.rec === this && (v.purpose === 'answer' || v.purpose === 'turn');
  };

  // Who hears the phrase: Whisper alone, or the wake word detector first. Over a
  // reading in Jarvis mode, `waking` says (#3798).
  HostRecognition.prototype.wake = function () {
    if (this.waking) return this.waking;
    return this.v.purpose === 'wake' ? this.v.wake : 'whisper';
  };

  // The host's answer for a recorded phrase, as a promise of its JSON.
  HostRecognition.prototype.request = function (body) {
    var self = this;
    var v = this.v;
    warmFast(v);
    // The language of the last words the host made out, once there are some: the
    // first phrase is the host's own call.
    var hint = v.heardLang ? '?language=' + v.heardLang : '';
    // A slow phrase asks whether the model is still loading, and says so (#3766).
    var stopWatch = shared.watchSlow(function () { return self.aborted; }, function (seconds, loading) {
      if (loading) {
        self.loading = true;
        loadingStatus(v);
      } else if (!seconds && self.loading) {
        self.loading = false;
        listeningStatus(v);
      }
    });
    var asked = hostRequest(v, '/api/speech/transcribe' + hint, {
      method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: body
    }, function () { return !self.aborted; }, function () {
      if (v.mode === 'listening') showStatus(v.purpose === 'question' ? 'Listening for your answer…' : 'Listening…');
    }).then(function (response) {
      stopWatch();
      return response.json();
    }, function (error) {
      stopWatch();
      throw error;
    });
    // Kept, so a phrase asked early can be sent again while the model loads (#3766).
    asked.body = body;
    // An early pass dropped as sound came back fails unseen.
    asked.catch(function () { /* the phrase's own pass reports it */ });
    return asked;
  };

  // The host's words for a phrase. `wake` names a waiting phrase for the wake log,
  // and says whether the detector already woke on it; `asked` is a request already
  // made early in its pause.
  HostRecognition.prototype.transcribe = function (body, wake, asked) {
    var self = this;
    var v = this.v;
    body = body || asked.body;
    (asked || this.request(body)).then(function (heard) {
      if (self.aborted) return;
      var text = (heard.text || '').trim();
      if (wake) logWake(wake, text);
      if (text) {
        // Whisper can call English sound Finnish and still write English words: the
        // words win.
        v.lang = v.heardLang = v.always || wordsLanguage(text) || (heard.language === 'fi' ? 'fi' : 'en');
        self.heard(text, !!(wake && wake.detected), doubtSpans(text, heard.segments));
      }
      self.end();
    }).catch(function () {
      if (self.aborted) return;
      modelLoading().then(function (loading) {
        if (self.aborted) return;
        self.loadingTries = (self.loadingTries || 0) + 1;
        if (loading && self.loadingTries <= LOADING_RETRIES) {
          loadingStatus(v);
          setTimeout(function () { if (!self.aborted) self.transcribe(body, wake); }, LOADING_RETRY_MS);
          return;
        }
        if (self.onerror) self.onerror({ error: 'host-failed' });
        self.end();
      });
    });
  };

  // Whether the speech service says it is still loading its model (#3766): false
  // when it says it is loaded, or cannot be asked at all.
  function modelLoading() {
    return fetch('/api/speech/status').then(function (response) {
      return response.ok ? response.json() : null;
    }).then(function (state) {
      return !!state && state.loaded === false;
    }, function () { return false; });
  }

  function loadingStatus(v) {
    if (voice === v && (v.mode === 'listening' || v.mode === 'waiting')) showStatus(LOADING_MODEL);
  }

  function listeningStatus(v) {
    if (voice !== v) return;
    if (v.mode === 'listening') showStatus(v.purpose === 'question' ? 'Listening for your answer…' : 'Listening…');
    else if (v.mode === 'waiting') showStatus(WAITING);
  }

  // One phrase's words, as a recognizer's final result. `woke`: the detector heard
  // the wake word in it, whatever Whisper wrote; `doubts`: where the parts Whisper
  // doubted sit in `text`.
  HostRecognition.prototype.heard = function (text, woke, doubts) {
    var result = [{ transcript: text }];
    result.isFinal = true;
    result.woke = woke;
    result.doubts = doubts || [];
    if (this.onresult) this.onresult({ resultIndex: 0, results: [result] });
  };

  // What Whisper made of a waiting phrase, for the wake log; best effort.
  function logWake(wake, text) {
    fetch('/api/speech/wake-log', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: wake.id, text: text, matched: WAKE_WORD.test(text), ms: Date.now() - wake.ended,
        detected: !!wake.detected
      })
    }).catch(function () { /* only logged */ });
  }

  // Whether the phrase heard so far has words in it, without ending it: `done(true,
  // text)` or `done(false)`. A failed check counts as no words, so the send is not
  // held.
  HostRecognition.prototype.probe = function (done) {
    var v = this.v;
    var cap = v.capture;
    var phrase = this.sofar ? this.sofar() : [];
    if (!cap || !phrase.length) { done(false); return; }
    var hint = v.heardLang ? '?language=' + v.heardLang : '';
    fetch('/api/speech/transcribe' + hint, {
      method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: wav(phrase, cap.rate)
    }).then(function (response) {
      if (!response.ok) throw new Error('speech ' + response.status);
      return response.json();
    }).then(function (heard) {
      var text = (heard.text || '').trim();
      done(!!text, text);
    }).catch(function () { done(false); });
  };

  // A tap meaning "done" (#3787): the phrase being said ends now, as if paused.
  // False when no phrase has started.
  HostRecognition.prototype.cut = function () {
    var phrase = !this.done && this.sofar ? this.sofar() : [];
    if (!phrase.length) return false;
    this.endedAt = Date.now();
    this.finish(phrase);
    return true;
  };

  HostRecognition.prototype.end = function () {
    var self = this;
    setTimeout(function () { if (self.onend) self.onend(); }, 0);
  };

  HostRecognition.prototype.abort = function () {
    if (this.aborted) return;
    this.aborted = true;
    this.done = true;
    var cap = this.v.capture;
    if (cap) { cap.sink = null; cap.keep = false; cap.carry = []; }
    this.end();
  };

  function closeMicrophone(v) {
    v.micAsk = (v.micAsk || 0) + 1;
    if (v.capture) v.capture.close();
    v.capture = null;
    v.captureReady = null;
  }

  // Opened inside the 🎤 tap; listening waits for it.
  // Each open is numbered; closing the microphone (🎤 off, voice mode off) moves the
  // number on, so a permission that answers later stops its stream at once.
  function openMicrophone(v) {
    var ask = v.micAsk = (v.micAsk || 0) + 1;
    function stale() { return voice !== v || v.micAsk !== ask; }
    v.captureReady = navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    }).then(function (stream) {
      if (stale()) { stream.getTracks().forEach(function (t) { t.stop(); }); return; }
      v.capture = new Capture(v.audio, stream);
    }).catch(function () {
      if (!stale()) off('Microphone blocked');
    });
  }

  // ---- listening ----------------------------------------------------------

  // A chat kept "thinking" only by a helper agent or command it left running in the
  // background has ended its turn: its reply is read now, not when that work ends (#3529).
  function working() {
    var tag = document.querySelector('#session-status .status-tag');
    return !!(tag && tag.classList.contains('thinking') && !tag.hasAttribute('data-background'));
  }

  function dictating() {
    return !!document.querySelector('[data-speech-button][aria-pressed="true"]');
  }

  function words(text) {
    return text.toLowerCase().replace(/[^\p{L}\p{N}\s]+/gu, ' ').trim().split(/\s+/)
      .filter(Boolean);
  }

  function heard(v) {
    return v.phrases.join(' ').trim();
  }

  // Where the segments Whisper doubted sit in `text`, the host's words for a phrase,
  // as [start, end] pairs (#3817). `segments` are its parts in order, each
  // `{text, unsure}`; each is found after the one before, so the same words said
  // twice are told apart.
  function doubtSpans(text, segments) {
    var spans = [];
    var at = 0;
    (Array.isArray(segments) ? segments : []).forEach(function (segment) {
      var part = String((segment && segment.text) || '').trim();
      var found = part ? text.indexOf(part, at) : -1;
      if (found < 0) return;
      if (segment.unsure === true) spans.push([found, found + part.length]);
      at = found + part.length;
    });
    return spans;
  }

  // `spans` moved `by` characters and kept inside `length`, empty ones dropped.
  function moveSpans(spans, by, length) {
    var moved = [];
    (spans || []).forEach(function (span) {
      var start = Math.max(0, span[0] + by);
      var end = Math.min(length, span[1] + by);
      if (end > start) moved.push([start, end]);
    });
    return moved;
  }

  // The words after the wake word `name` found in `text`, and their doubts.
  function afterWake(text, doubts, name) {
    var tail = text.slice(name.index + name[0].length);
    var message = tail.trim();
    var cut = text.length - tail.replace(/^\s+/, '').length;
    return { text: message, doubts: moveSpans(doubts, -cut, message.length) };
  }

  // A phrase's words joined to the ones heard so far, its doubts with them.
  function addPhrase(v, text, doubts) {
    var before = v.phrases.join(' ');
    var by = before.length + (v.phrases.length ? 1 : 0);
    v.phrases.push(text);
    v.doubts = (v.doubts || []).concat(moveSpans(doubts, by, by + text.length));
  }

  // `text` with each doubted span as "[unsure: …]" (#3817).
  function markUnsure(text, doubts) {
    var spans = (doubts || []).slice().sort(function (a, b) { return b[0] - a[0]; });
    var end = text.length;
    spans.forEach(function (span) {
      if (span[1] > end) return;  // overlaps one already marked
      text = text.slice(0, span[0]) + '[unsure: ' + text.slice(span[0], span[1]) + ']' +
        text.slice(span[1]);
      end = span[0];
    });
    return text;
  }

  function clearTimers(v) {
    clearTimeout(v.sendTimer);
    clearTimeout(v.giveUpTimer);
    clearTimeout(v.holdTimer);
    v.sendTimer = null;
    v.giveUpTimer = null;
    v.holdTimer = null;
  }

  // `purpose` is 'answer' (after a reply), 'turn' (while the agent works),
  // 'question' (a question box) or 'wake' (waiting for "Hey Jarvis" once no words
  // came for `v.idleMs`, listening through the host).
  function listen(purpose) {
    var v = current();
    if (!v) return;
    // A phrase still with the fast agent: its answer listens (#3444).
    if (v.relaying) { v.relayPurpose = purpose; return; }
    // The deck chat's jump waited for its turn's reply to be read (#3816).
    if (v.jump && !working() && !v.question) { jumpNow(v); return; }
    var interrupted = v.interrupted;
    v.interrupted = '';
    v.doneTap = false;
    // A tap on 🎤, or "Hey Jarvis" alone, listens for one message in any mode (#3540).
    var once = v.once;
    v.once = false;
    // Woken over a 🔊 note read while it waited, it listens for the message (#3798).
    if (once && purpose === 'wake') purpose = working() ? 'turn' : 'answer';
    stopListening(v);
    if (dictating()) { rest('Dictation is on'); return; }
    if (document.hidden && v.blocked.listen) { background(v, 'listen'); return; }
    // Listening openly answers a question box unasked; the room state waits for the
    // name and takes the words after it as the answer, by the owner's decision on
    // #3540, or for a tap on 🎤; with 🎤 off it rests until a tap answers it.
    var asked = purpose === 'question' && !once && !(v.question && v.question.tapped);
    if (asked && v.hears && v.room) purpose = 'wake';
    else if (asked && !v.hears) {
      v.purpose = purpose;
      if (!v.hears) closeMicrophone(v);
      rest(TAP_TO_ANSWER);
      return;
    }
    // 🎤 off: the microphone is let go of, so the
    // browser's recording light goes out too; with 🎧 off as well, voice mode ends.
    if (purpose !== 'question' && !once && !v.hears) {
      v.purpose = purpose;
      closeMicrophone(v);
      if (!v.reads) { off(''); return; }
      setMode('idle');
      showStatus('');
      return;
    }
    if (purpose !== 'question' && !once && v.room) purpose = 'wake';
    v.purpose = purpose;
    dropEarly(v, 'not sent: listening started over');
    v.phrases = interrupted ? [interrupted] : [];
    v.doubts = interrupted ? v.interruptedDoubts || [] : [];
    v.woke = false;
    v.wordsAt = Date.now();
    v.base = v.input.value.replace(/\s+$/, '');
    v.quickEnds = 0;
    if (purpose === 'wake') {
      setMode('waiting');
      showStatus(WAITING);
      run(v);
      return;
    }
    setMode('listening');
    showStatus(purpose === 'question' ? 'Listening for your answer…' : 'Listening…');
    tone(880);
    idle(v);
    if (interrupted) v.hearing = false;
    run(v);
    // Words said over the reading are sent like any heard, unless more follow.
    if (interrupted) {
      show(v);
      if (!v.hearing) v.sendTimer = setTimeout(function () { finish(v); }, HOST_SEND_AFTER_MS);
    }
  }

  // Gives up once no words have come for `v.idleMs`; words heard start it again.
  // It then waits for "Hey Jarvis" with the microphone kept open, by the owner's
  // decision on #3424; a question box rests.
  function idle(v) {
    clearTimeout(v.giveUpTimer);
    function giveUp() {
      if (voice !== v || v.mode !== 'listening') return;
      // Words waiting to be sent, or a phrase the host is still hearing, go first.
      if (heard(v) || v.hearing) { v.giveUpTimer = setTimeout(giveUp, SEND_AFTER_MS); return; }
      tone(330);
      if (v.purpose !== 'question') { listen('wake'); return; }
      stopListening(v);
      rest(TAP_TO_ANSWER, true);
    }
    v.giveUpTimer = setTimeout(giveUp, v.idleMs);
  }

  function run(v) {
    var rec = new HostRecognition(v);
    v.rec = rec;
    v.startedAt = Date.now();
    rec.onresult = function (event) {
      if (current() !== v || v.rec !== rec) return;
      v.quickEnds = 0;
      idle(v);
      addPhrase(v, event.results[0][0].transcript.trim(), event.results[0].doubts);
      if (event.results[0].woke) v.woke = true;
      // More words joined the phrase: the fast agent's answer so far is not its.
      if (v.early && v.early.text !== heard(v)) dropEarly(v, 'not sent: more words followed');
      v.clock = { end: rec.endedAt || Date.now(), words: Date.now() };
      v.hearing = false;
      show(v);
      clearTimeout(v.sendTimer);
      clearTimeout(v.holdTimer);
      v.wordsAt = Date.now();
      if (heard(v)) v.sendTimer = setTimeout(function () { finish(v); }, HOST_SEND_AFTER_MS);
    };
    // Speech starting again holds back the send. Noise must not hold it for good
    // (#3449): a phrase that ends with no words lets it go (`onend`), and one still
    // going after HOST_HOLD_CHECK_MS is checked for words.
    rec.onspeechstart = function () {
      if (current() !== v || v.rec !== rec) return;
      v.hearing = true;
      clearTimeout(v.sendTimer);
      clearTimeout(v.holdTimer);
      if (!heard(v)) return;
      v.holdTimer = setTimeout(function () {
        if (current() !== v || v.rec !== rec || !v.hearing) return;
        rec.probe(function (words) {
          if (current() !== v || v.rec !== rec || !v.hearing || words) return;
          finish(v);
        });
      }, HOST_HOLD_CHECK_MS);
    };
    rec.onerror = function (event) {
      if (current() !== v || v.rec !== rec) return;
      if (event.error === 'host-failed') { v.failed = UNREACHABLE; return; }
      // Hidden, a stalled microphone is the browser refusing to listen in the
      // background: our own aborts never get here, because they let go of `v.rec`.
      if (document.hidden) v.failed = 'background';
    };
    // Each listener ends after one phrase; listening goes on until sent.
    rec.onend = function () {
      if (current() !== v || v.rec !== rec) return;
      v.rec = null;
      v.hearing = false;
      var why = v.failed;
      v.failed = null;
      if (why === 'background') { background(v, 'listen'); return; }
      if (why) { off(why); return; }
      if (!listeningMode(v)) return;
      // Tapped done (#3787): what the phrase brought goes at once, and with nothing
      // it goes back to waiting for "Hey Jarvis".
      var tapped = v.doneTap;
      v.doneTap = false;
      // A phrase that held the send back and brought no words (a cough, a fan, a
      // dropped "Thank you.") lets it go when it was due: a second after the last
      // words, not after the noise, or a run of coughs would put it off for good.
      // Already due, it goes now: `run` below replays sound kept while this phrase
      // was transcribed, and the next cough in it would cancel a timer.
      clearTimeout(v.holdTimer);
      if (heard(v)) {
        clearTimeout(v.sendTimer);
        // The detector heard "Hey Jarvis" (#3793): it answers at once. Waiting a
        // second for more words gained nothing, since a waiting phrase without the
        // name is let go of, and made the beep come late.
        var now = tapped || (v.woke && v.purpose === 'wake');
        var due = now ? 0 : v.wordsAt + HOST_SEND_AFTER_MS - Date.now();
        if (due > 0) v.sendTimer = setTimeout(function () { finish(v); }, due);
        else finish(v);
        // Sent, the phrase is with the fast agent: no listener may write a later
        // phrase into the draft it is sent from (#3514). One its answer started
        // already listens.
        if (!listeningMode(v) || v.rec) return;
      } else if (tapped) {
        listen(v.question ? 'question' : working() ? 'turn' : 'answer');
        return;
      }
      if (Date.now() - v.startedAt < QUICK_END_MS && !heard(v)) {
        v.quickEnds += 1;
        if (v.quickEnds > QUICK_ENDS_ALLOWED) {
          if (document.hidden) background(v, 'listen');
          else rest('Listening stopped · tap 🎤');
          return;
        }
      }
      run(v);
      // Nothing heard in the sound kept while this phrase was transcribed: the fast
      // agent is asked now, not when the words are sent (#3765).
      if (v.rec && !v.hearing) askEarly(v);
    };
    try {
      rec.start();
    } catch (e) {
      v.rec = null;
      if (document.hidden) background(v, 'listen');
      else rest('Could not start listening');
    }
  }

  function show(v) {
    if (v.purpose === 'wake') return;
    var text = heard(v);
    if (v.purpose === 'question') {
      showStatus(text ? 'Heard: ' + text : 'Listening for your answer…');
      return;
    }
    var value = text ? v.base + (v.base ? ' ' : '') + text : v.base;
    if (v.input.value !== value) {
      v.input.value = value;
      v.written = value;
      v.input.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }

  // Typing while it listens keeps what was typed: the draft so far becomes the new
  // start, the words already heard stay in it, and nothing is sent until speech
  // starts again.
  document.addEventListener('input', function (event) {
    var v = voice;
    if (!v || event.target !== v.input || v.mode !== 'listening') return;
    // Only the person's own typing: the composer clearing itself after a send, and
    // this script's own writes, are not edits to keep.
    if (!event.isTrusted || v.input.value === v.written) return;
    v.base = v.input.value.replace(/\s+$/, '');
    v.written = v.input.value;
    v.phrases = [];
    v.doubts = [];
    dropEarly(v, 'not sent: typed over');
    clearTimeout(v.sendTimer);
    v.sendTimer = null;
  }, true);

  function stopListening(v) {
    clearTimers(v);
    var rec = v.rec;
    v.rec = null;
    if (rec) {
      try { rec.abort(); } catch (e) { /* already ended */ }
    }
    if (listeningMode(v)) setMode('idle');
  }

  // Listening for a message or an answer, or waiting for "Hey Jarvis".
  function listeningMode(v) {
    return v.mode === 'listening' || v.mode === 'waiting';
  }

  // `idled`: it rests because no words came, so a 🔊 note is still read (#3456).
  function rest(text, idled) {
    var v = voice;
    if (!v) return;
    v.idled = !!idled;
    stopListening(v);
    setMode('resting');
    showStatus(text);
  }

  // The browser will not `what` ('listen' or 'speak') with the page hidden: pause
  // as before, say which, and do not try it hidden again while voice mode stays on.
  // A reply cut off before it was read stays unread for the tap that resumes.
  function background(v, what) {
    if (voice !== v) return;
    if (what !== 'frozen') v.blocked[what] = true;
    if (v.reading) {
      v.spokenId = v.reading.before;
      if (v.reading.note) v.noted = false;
    }
    v.reading = null;
    stopSpeaking(v);
    rest(BACKGROUND[what]);
  }

  // A tap after a pause: what arrived meanwhile and was not read first — the
  // reply, or while the agent still works its 🔊 note — then on.
  function resume(v) {
    if (v.question) { askNext(v); return; }
    var reply = lastReply();
    var unread = reply && reply.id !== v.spokenId;
    if (unread && !working()) {
      v.replyDeadline = 0;
      speakReply(v);
      return;
    }
    if (unread && !v.noted) {
      setMode('idle');
      speakNote(v);
      if (v.mode === 'speaking') return;
    }
    listen(working() ? 'turn' : 'answer');
  }

  function restoreDraft(v) {
    if (v.input.value !== v.base) {
      v.input.value = v.base;
      v.input.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }

  // Two seconds of silence after speech: act on what was heard.
  function finish(v) {
    if (voice !== v || !listeningMode(v)) return;
    var text = heard(v);
    if (!text) return;
    var said = words(text).join(' ');
    v.phrases = [];
    clearTimeout(v.sendTimer);
    // The fast agent asked early for these very words, with no 🔊 line read since.
    var early = v.early;
    v.early = null;
    if (early && (early.text !== text || early.aloud !== v.aloud.length || !relayed(v, said))) {
      v.early = early;
      dropEarly(v, 'not sent: the words changed');
      early = null;
    }
    var doubts = v.doubts || [];
    v.doubts = [];
    if (v.purpose === 'wake') { woken(v, text, doubts); return; }
    var marked = markUnsure(text, doubts);
    if (v.purpose === 'question') { answerQuestion(v, text, doubts.length > 0); return; }
    if (ROOM_PHRASE.test(said)) {
      restoreDraft(v);
      v.room = true;
      v.hears = true;
      remember(v);
      speak(ownLine('room'), function () { listen(working() ? 'turn' : 'answer'); });
      return;
    }
    if (said === 'cancel') {
      restoreDraft(v);
      tone(330);
      showStatus('Cancelled · listening…');
      return;
    }
    if (said === 'stop' && working()) {
      restoreDraft(v);
      var stop = document.querySelector('#composer-controls .stop-button');
      var interrupt = document.getElementById('interrupt-form');
      if (stop) stop.click();
      else if (interrupt) interrupt.requestSubmit();
      tone(440);
      showStatus('Stopping the turn…');
      return;
    }
    var send = v.form.querySelector('#composer-controls button[type="submit"]:not(.stop-button)');
    if (!send || send.disabled) { showStatus('Could not send · on screen'); return; }
    tone(660);
    showStatus('Heard · passing it on…');
    // One phrase at a time: listening waits for the fast agent's answer, so no
    // later phrase is written into the draft this one is sent from.
    var base = v.base;
    var shown = v.input.value;
    stopListening(v);
    v.relaying = true;
    var asked = early ? early.at : Date.now();
    // Names this phrase in the voice log, so what the page really sent is saved
    // beside what the fast agent proposed (#3475).
    var id = early ? early.id : phraseId(asked);
    if (early) v.aloud.splice(0);
    var clock = v.clock;
    if (clock) { clock.id = id; clock.sent = Date.now(); timed(v, clock); }
    v.relayPurpose = working() ? 'turn' : 'answer';
    // What the person had not heard when they stopped speaking (#3520).
    var unheard = unheardNow(v);
    var last = v.lastAloud;
    answered(early || startAsk(v, text, id, false, marked), function (answer) {
      v.relaying = false;
      if (current() !== v) return;
      var after = function () {
        if (v.mode === 'speaking' || v.question) return;
        listen(working() ? 'turn' : (v.relayPurpose || 'answer'));
      };
      if (answer && answer.jump) {
        // The way back to the deck chat (#3816): the words go nowhere, the page goes there.
        v.input.value = v.input.value === shown ? base : v.input.value;
        v.input.dispatchEvent(new Event('input', { bubbles: true }));
        reportSent(v, id, null, 'not sent: back to the deck chat');
        showStatus('Back to the deck chat…');
        var back = answer.jump;
        v.jump = { title: back.title, go: function () { goTo(back.path); } };
        jumpNow(v);
        return;
      }
      if (answer && answer.relay === null) {
        // Plain noise: nothing is sent and nothing said; the draft is what it was
        // with anything typed meanwhile.
        v.input.value = v.input.value === shown ? base : v.input.value;
        v.input.dispatchEvent(new Event('input', { bubbles: true }));
        showStatus('Not sent · listening…');
        reportSent(v, id, null, 'not sent: taken as noise');
        after();
        return;
      }
      if ((v.sentAt || 0) >= asked) {
        // Sent by hand meanwhile: that send carried the draft, so this one would
        // send it twice. The hand send wins (the owner's decision on #3448).
        showStatus('Sent · listening…');
        reportSent(v, id, null, 'not sent: sent by hand meanwhile');
        after();
        return;
      }
      var words = answer ? answer.relay : marked;
      // What was typed meanwhile is kept: after the words, or, edited into them,
      // in place of them.
      var now = v.input.value;
      var value = base + (base ? ' ' : '') + words;
      if (now !== shown) value = now.indexOf(shown) === 0 ? value + now.slice(shown.length) : now;
      v.input.value = value;
      v.input.dispatchEvent(new Event('input', { bubbles: true }));
      var button = v.form.querySelector(
        '#composer-controls button[type="submit"]:not(.stop-button)');
      if (!button || button.disabled) {
        showStatus('Could not send · on screen');
        reportSent(v, id, null, 'not sent: could not send, left on screen');
        after();
        return;
      }
      // Its own line is said only if the chat stays silent (#3799).
      v.fastSay = answer && answer.say ? { text: answer.say, id: id } : null;
      v.spoken = { unheard: unheard, last: last, at: Date.now() };
      button.click();
      v.fastSay = null;
      reportSent(v, id, value, answer ? 'sent' : 'sent as heard: no answer in time');
      v.base = '';
      showStatus(working() ? 'Sent as a follow-up · listening…' : 'Sent');
      after();
    });
  }

  function phraseId(at) {
    return at.toString(36) + Math.random().toString(36).slice(2, 8);
  }

  // Whether a phrase whose words are `said` goes to the chat through the fast agent,
  // rather than being a command of voice mode's own.
  function relayed(v, said) {
    if (v.purpose !== 'answer' && v.purpose !== 'turn') return false;
    return !ROOM_PHRASE.test(said) && said !== 'cancel' && !(said === 'stop' && working());
  }

  // The fast agent asked as soon as the words come back (#3765), with the send still
  // a second away: `finish` takes its answer when the words are still the same.
  // The 🔊 lines it is told stay for the next ask until then.
  function askEarly(v) {
    if (voice !== v || v.relaying || v.mode !== 'listening') return;
    var text = heard(v);
    if (!text || (v.early && v.early.text === text)) return;
    dropEarly(v, 'not sent: more words followed');
    if (!relayed(v, words(text).join(' '))) return;
    var at = Date.now();
    v.early = startAsk(v, text, phraseId(at), true, markUnsure(text, v.doubts));
    v.early.at = at;
  }

  // An early answer not taken: the voice log says why, and the next ask tells the
  // fast agent to forget it.
  function dropEarly(v, why) {
    var early = v.early;
    if (!early) return;
    v.early = null;
    v.dropped = early.id;
    reportSent(v, early.id, null, why);
  }

  // Asks the fast agent about `text` (#3444); `answered` hears its answer. `keep`
  // leaves the 🔊 lines it is told for the next ask. `marked` is `text` as the fast
  // agent is told it, its doubted parts marked (#3817).
  function startAsk(v, text, id, keep, marked) {
    var ask = { text: text, id: id, at: Date.now(), aloud: v.aloud.length, done: false,
      answer: null, waiting: [] };
    var aloud = keep ? v.aloud.slice() : v.aloud.splice(0);
    var drop = v.dropped || '';
    v.dropped = '';
    var timer = setTimeout(function () { finished(null); }, FAST_WAIT_MS);
    function finished(answer) {
      if (ask.done) return;
      ask.done = true;
      ask.answer = answer;
      clearTimeout(timer);
      ask.waiting.splice(0).forEach(function (then) { then(answer); });
    }
    fetch('/api/voice/heard', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_key: v.key, text: marked || text, aloud: aloud, id: id,
        run: v.run, drop: drop })
    }).then(function (reply) { return reply.ok ? reply.json() : null; })
      .then(function (body) {
        if (!body || (typeof body.relay !== 'string' && body.relay !== null)) {
          finished(null);
          return;
        }
        var relay = body.relay === null ? null : body.relay.trim() || marked || text;
        var jump = body.jump && typeof body.jump.path === 'string' ? body.jump : null;
        finished({ say: speakable(String(body.say || '')), relay: relay, jump: jump });
      }, function () { finished(null); });
    return ask;
  }

  // The fast agent's answer to an ask: `{say, relay}`, relay null for noise; or null
  // when it has none in time.
  function answered(ask, then) {
    if (ask.done) { then(ask.answer); return; }
    ask.waiting.push(then);
  }

  // ---- how long a phrase waited (#3765) -------------------------------------

  // The first voice heard after a phrase's words: the first reading.
  function heardVoice(v) {
    var clock = v.clock;
    if (!clock || clock.voice) return;
    if (clock.sent && Date.now() - clock.sent > VOICE_TIMED_MS) { v.clock = null; return; }
    clock.voice = Date.now();
    timed(v, clock);
  }

  // Saved in the voice log once the phrase is sent and its first voice was heard.
  function timed(v, clock) {
    if (!clock.id || !clock.voice || clock.reported) return;
    clock.reported = true;
    try {
      fetch('/api/voice/sent', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify({ session_key: v.key, id: clock.id, timing: {
          words: clock.words - clock.end, sent: clock.sent - clock.end,
          voice: clock.voice - clock.end
        } })
      }).catch(function () { /* only measured */ });
    } catch (e) { /* only measured */ }
  }

  // Starts the fast agent while the phrase is still being transcribed (#3507), so
  // it is not started on the phrase's own time; the server ignores a running one,
  // and one for a spell (`run`) whose end it already had.
  function warmFast(v) {
    if (voice !== v) return;
    try {
      fetch('/api/voice/warm', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_key: v.key, run: v.run })
      }).catch(function () { /* the phrase starts it instead */ });
    } catch (e) { /* the phrase starts it instead */ }
  }

  // What the page really did with a phrase, for the voice log (#3475): the text the
  // composer sent, or null and why nothing was. Told when Send is pressed, not when
  // the send lands: a failed send still reads as sent, a limit the owner accepted.
  function reportSent(v, id, text, how) {
    try {
      fetch('/api/voice/sent', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify({ session_key: v.key, id: id, text: text, how: how })
      }).catch(function () { /* the log then says it was not reported */ });
    } catch (e) { /* the log then says it was not reported */ }
  }

  // Something in the chat not read aloud: a reply, one cut off by the person talking
  // over it, or a turn not started from here.
  function unheardNow(v) {
    return unreadReply(v) || !!v.cutReading || (working() && !!v.unheardTurn);
  }

  // Only a yes or a no, the words a phantom phrase most often is (#3520).
  var SHORT_ANSWER = ('yes yeah yep yup ya ok okay sure right no nope nah juu joo jep ' +
    'kyllä okei selvä ei go ahead do it please').split(' ');
  function shortAnswer(text) {
    var said = words(text);
    return !!said.length && said.every(function (w) { return SHORT_ANSWER.indexOf(w) >= 0; });
  }

  // A 🔊 line read aloud, for the fast agent's memory.
  function readAloud(v, text) {
    v.aloud.push(text);
    if (v.aloud.length > ALOUD_KEPT) v.aloud.shift();
  }

  // A phrase heard while waiting: anything without "Hey Jarvis" is let go of; the
  // words after it are a message, as if heard while listening for one. A phrase the
  // detector woke on counts even when Whisper wrote the name some other way: then
  // all of its words are the message.
  function woken(v, text, doubts) {
    var woke = v.woke;
    v.woke = false;
    var match = WAKE_WORD.exec(text);
    if (!match && !woke) return;
    var message = text;
    if (match) {
      var after = afterWake(text, doubts, match);
      message = after.text;
      doubts = after.doubts;
    }
    // The name alone listens at once: the listening beep is its only answer, so no
    // spoken line is there for the microphone to hear (#3781).
    if (!message) {
      v.once = true;
      listen(v.question ? 'question' : working() ? 'turn' : 'answer');
      return;
    }
    // A question box waiting in Hey Jarvis mode: the words after the name answer it.
    if (v.question) {
      setMode('listening');
      v.purpose = 'question';
      answerQuestion(v, message, doubts.length > 0);
      return;
    }
    v.purpose = working() ? 'turn' : 'answer';
    v.base = v.input.value.replace(/\s+$/, '');
    setMode('listening');
    showStatus('Listening…');
    v.phrases = [message];
    v.doubts = doubts;
    show(v);
    finish(v);
  }

  // ---- question boxes -----------------------------------------------------

  function questionForm() {
    return document.querySelector('#pending-interaction form[data-interaction-kind]');
  }

  // The stream re-sends the same question on a reconnect; it is asked once.
  function askQuestions(form) {
    var v = current();
    var id = form.querySelector('input[name="interaction_id"]');
    var key = id ? id.value : form;
    if (!v) return;
    if (v.asked === key) {
      // The same box swapped in again: the answers given so far are carried into
      // it, and the rest go into the one on the page.
      var q = v.question;
      if (q && q.form !== form) {
        carryAnswers(q.form, form);
        q.form = form;
        q.sets = Array.prototype.slice.call(form.querySelectorAll('fieldset'));
      }
      return;
    }
    v.asked = key;
    stopListening(v);
    var kind = form.getAttribute('data-interaction-kind');
    var heading = form.closest('#pending-interaction').querySelector('h2');
    var title = heading ? heading.textContent.trim() : 'The agent asks something';
    var sets = form.querySelectorAll('fieldset');
    // A Codex MCP form asks questions the same way, through the same Submit.
    if ((kind !== 'question' && kind !== 'mcp_form') || !sets.length) {
      var lang = boxLanguage(v, title);
      speak(title + '. ' + SAID[lang].onScreen, function () { rest('Answer on screen'); }, lang);
      return;
    }
    v.question = { form: form, sets: Array.prototype.slice.call(sets), index: 0, retried: false,
      tapped: !!v.tapQuestion };
    askNext(v);
  }

  // What was ticked or typed in `from`, set in the same fields of `to`, as if picked
  // there, so the box's own draft keeping sees it too.
  function carryAnswers(from, to) {
    Array.prototype.forEach.call(from.querySelectorAll('input'), function (old) {
      if (!old.name || old.type === 'hidden' || old.type === 'password') return;
      var fresh = Array.prototype.filter.call(to.querySelectorAll('input'), function (input) {
        return input.name === old.name && input.type === old.type &&
          (old.type === 'text' || input.value === old.value);
      })[0];
      if (!fresh) return;
      if (old.type === 'text') {
        if (!old.value || fresh.value === old.value) return;
        fresh.value = old.value;
      } else {
        if (!old.checked || fresh.checked) return;
        fresh.checked = true;
      }
      fresh.dispatchEvent(new Event('input', { bubbles: true }));
      fresh.dispatchEvent(new Event('change', { bubbles: true }));
    });
  }

  function optionsOf(fieldset) {
    return Array.prototype.slice.call(fieldset.querySelectorAll('input[type=radio], input[type=checkbox]'));
  }

  function askNext(v) {
    var q = v.question;
    var fieldset = q.sets[q.index];
    if (!fieldset) {
      v.question = null;
      // An answer the box still lacks would block its Submit: not "Answer sent".
      if (q.form.checkValidity && !q.form.checkValidity()) {
        speak(ownLine('onScreen'), function () { rest('Answer on screen'); });
        return;
      }
      var submit = q.form.querySelector('button[name="decision"][value="accept"]');
      if (submit) submit.click();
      tone(660);
      showStatus('Answer sent');
      listen('turn');
      return;
    }
    if (fieldset.hasAttribute('data-secret') || fieldset.querySelector('input[type="password"]')) {
      v.question = null;
      speak(ownLine('secret') + ' ' + ownLine('onScreen'), function () { rest('Answer on screen'); });
      return;
    }
    var prompt = fieldset.querySelector('.interaction-prompt');
    var text = speakable(prompt ? prompt.textContent : fieldset.textContent);
    var labels = optionsOf(fieldset).map(function (input) {
      var label = input.closest('label');
      var strong = label && label.querySelector('strong');
      return speakable(strong ? strong.textContent : input.value);
    });
    var lang = boxLanguage(v, [text].concat(labels).join(' '));
    var lines = [text];
    labels.forEach(function (label, i) {
      lines.push(SAID[lang].option + ' ' + (i + 1) + ', ' + label + '.');
    });
    speak(lines.join(' '), function () { listen('question'); }, lang);
  }

  // A question box is read in the language it is written in, even for an owner who
  // speaks only Finnish: the Finnish voice cannot read an English question (#3572).
  function boxLanguage(v, text) {
    return wordsLanguage(text) || v.always || 'en';
  }

  function numbersIn(said, count) {
    var tokens = words(said).filter(function (w) {
      return ['option', 'options', 'number', 'numbers', 'and', 'vaihtoehto', 'numero', 'ja']
        .indexOf(w) < 0;
    });
    if (!tokens.length) return null;
    var picked = [];
    for (var i = 0; i < tokens.length; i++) {
      var n = /^\d+$/.test(tokens[i]) ? parseInt(tokens[i], 10) : NUMBER_WORDS[tokens[i]];
      if (!n || n > count) return null;
      if (picked.indexOf(n) < 0) picked.push(n);
    }
    return picked;
  }

  // `doubted`: Whisper doubted some of the words, so nothing is picked from them and
  // the answer is asked for again, once (the owner's decision on #3817).
  function answerQuestion(v, text, doubted) {
    var q = v.question;
    if (!q || !q.form.isConnected) { v.question = null; rest(''); return; }
    var fieldset = q.sets[q.index];
    var options = optionsOf(fieldset);
    var other = fieldset.querySelector('input.interaction-other');
    var said = words(text).join(' ');
    if (said === 'cancel') {
      v.question = null;
      speak(ownLine('onScreen'), function () { rest('Answer on screen'); });
      return;
    }
    var picked = options.length && !doubted ? numbersIn(text, options.length) : null;
    var multi = options.length && options[0].type === 'checkbox';
    if (doubted && !q.retried) {
      q.retried = true;
      speak(ownLine('again'), function () { listen('question'); });
      return;
    } else if (doubted) {
      v.question = null;
      speak(ownLine('onScreen'), function () { rest('Answer on screen'); });
      return;
    } else if (picked && (multi || picked.length === 1)) {
      picked.forEach(function (n) {
        var option = options[n - 1];
        option.checked = true;
        option.dispatchEvent(new Event('change', { bubbles: true }));
      });
    } else if (other) {
      other.value = text;
      other.dispatchEvent(new Event('input', { bubbles: true }));
    } else if (!q.retried) {
      q.retried = true;
      speak(ownLine('number'), function () { listen('question'); });
      return;
    } else {
      v.question = null;
      speak(ownLine('onScreen'), function () { rest('Answer on screen'); });
      return;
    }
    q.index += 1;
    q.retried = false;
    tone(660);
    askNext(v);
  }

  // ---- turn changes -------------------------------------------------------

  function onStatus() {
    var v = current();
    if (!v) return;
    var now = working();
    var was = v.working;
    v.working = now;
    if (now && !was) {
      clearTimeout(v.replyTimer);
      // Not started by a send from here: what started it was never heard (#3520).
      v.unheardTurn = !v.awaiting;
      v.noted = false;
      // A microphone that already gave up waiting stays that way until the turn
      // ends (#3456), as does one resting; a send from here is acknowledged, and that
      // listens anyway.
      if (v.mode !== 'speaking' && v.mode !== 'waiting' &&
          v.mode !== 'resting' && !v.question) {
        if (v.mode === 'listening') v.purpose = 'turn';
        else listen('turn');
      }
      watchQuiet(v);
    } else if (!now && was) {
      // Its reply is read next; nothing more is said about its work.
      v.quiet = null;
      clearTimeout(v.quietTimer);
      // Words still being spoken are sent by their own 2 s of silence, not cut off here.
      waitForReply(v);
    }
  }

  function waitForReply(v) {
    v.awaiting = false;
    v.replyDeadline = Date.now() + REPLY_WAIT_MAX_MS;
    settleReply(v);
  }

  function settleReply(v) {
    clearTimeout(v.replyTimer);
    var wait = Math.min(REPLY_SETTLE_MS, Math.max(0, v.replyDeadline - Date.now()));
    v.replyTimer = setTimeout(function () { speakReply(v); }, wait);
  }

  // A reply to a message sent from here that lands with the page never having seen
  // the turn start: a turn of a few seconds can begin and end between two status
  // swaps, so its end is never seen either (#3506).
  function unreadReply(v) {
    var reply = lastReply();
    return !!reply && reply.id !== v.spokenId;
  }

  function speakReply(v) {
    if (current() !== v || working()) return;
    // A note still being read finishes first; its end listens, and this reads next.
    if (v.speaking && v.noting) {
      clearTimeout(v.replyTimer);
      v.replyTimer = setTimeout(function () { speakReply(v); }, 500);
      return;
    }
    // Speaking would cut off someone mid-answer: wait for their silence to send it.
    if (v.mode === 'listening' && (heard(v) || v.hearing)) {
      clearTimeout(v.replyTimer);
      v.replyTimer = setTimeout(function () { speakReply(v); }, SEND_AFTER_MS);
      return;
    }
    // Words said before this reply wait on the fast agent: it stays unread until
    // they are sent, so they go with the unheard warning (#3520).
    if (v.relaying) {
      clearTimeout(v.replyTimer);
      v.replyTimer = setTimeout(function () { speakReply(v); }, 500);
      return;
    }
    if (questionForm()) { v.replyDeadline = 0; return; }
    var reply = lastReply();
    if (!reply || reply.id === v.spokenId) {
      // The reply may still be on its way: a transcript swap settles again, and the
      // deadline is the last look.
      var left = v.replyDeadline - Date.now();
      if (left > 0) {
        clearTimeout(v.replyTimer);
        v.replyTimer = setTimeout(function () { speakReply(v); }, left);
        return;
      }
      v.replyDeadline = 0;
      // Already being read (a tap replayed it): its end listens, not a timer left
      // over from before (#3520).
      if (v.speaking) return;
      // A reply already read as the turn's note: a waiting jump goes now (#3816).
      if (v.jump && !v.question) { stopListening(v); jumpNow(v); return; }
      if (v.mode === 'listening') v.purpose = 'answer';
      else listen('answer');
      return;
    }
    v.replyDeadline = 0;
    // 🎧 off (#3540): nothing is read, and Always mode listens now the turn ended.
    if (!v.reads) {
      v.spokenId = reply.id;
      listen('answer');
      return;
    }
    v.reading = { before: v.spokenId, last: v.lastAloud };
    v.spokenId = reply.id;
    var text = spokenPart(reply.querySelector('.ev-text'));
    if (!text) { listen('answer'); return; }
    readAloud(v, text);
    speak(text, function () {
      heardOut(v, text);
      listen('answer');
    }, spokenLanguage(v, text));
  }

  // The voice for what the agent wrote: its own words' language, else the one the
  // last message asked for. What was heard since does not change it (#3417).
  function spokenLanguage(v, text) {
    return v.always || wordsLanguage(text) || v.answerLang || v.lang;
  }

  // The agent's first "🔊" note of a turn is read while it works. Someone mid-
  // sentence is not cut off: the note waits for their silence to send it. A short
  // turn's final reply landing before the status leaves "thinking" is read here
  // instead, once: its `spokenId` keeps the turn's end from reading it again.
  // After the note the microphone is as it was: one that gave up goes back to
  // waiting for "Hey Jarvis", or to resting, and opens
  // again only once the turn ends (#3456).
  function speakNote(v) {
    clearTimeout(v.noteTimer);
    if (current() !== v || !v.reads || !working() || v.noted || v.question) return;
    var reply = lastReply();
    if (!reply || reply.id === v.spokenId) return;
    var text = markedOpening(reply.querySelector('.ev-text'));
    if (!text) return;
    // A hidden page reads it too (#3408); a browser that will not is found by `speak`.
    if (v.mode === 'speaking' || (v.mode === 'resting' && !v.idled)) return;
    if (v.mode === 'listening' && (heard(v) || v.hearing)) {
      v.noteTimer = setTimeout(function () { speakNote(v); }, SEND_AFTER_MS);
      return;
    }
    if (v.relaying) {
      v.noteTimer = setTimeout(function () { speakNote(v); }, 500);
      return;
    }
    var asleep = v.mode === 'waiting';
    var idled = v.mode === 'resting';
    v.noted = true;
    v.reading = { before: v.spokenId, note: true, last: v.lastAloud };
    v.spokenId = reply.id;
    readAloud(v, text);
    speak(text, function () {
      heardOut(v, text);
      v.noting = false;
      // The turn ended meanwhile: its reply is read before anything listens (#3456).
      if (replyIfEnded(v)) return;
      if (working() && idled) rest('Voice mode on', true);
      else listen(!working() ? 'answer' : asleep ? 'wake' : 'turn');
    }, spokenLanguage(v, text));
    v.noting = true;
  }

  // A turn that ended while something was said in passing, with a reply not yet
  // read, reads it before listening (#3456). Words that talked over the reading,
  // or a short turn whose reply was the note just read, listen at once as before.
  function replyIfEnded(v) {
    if (current() !== v || working() || !v.replyDeadline || v.interrupted) return false;
    var reply = lastReply();
    if (!reply || reply.id === v.spokenId) return false;
    setMode('idle');
    showStatus('');
    settleReply(v);
    return true;
  }

  // A message just sent from here (#3444) is not answered at once (#3799): the chat
  // usually says so itself. Only when it stays silent does the fast agent speak;
  // `fast` (the fast agent's own line for the phrase and its id) is said when it has
  // nothing, else "Got it, working on it.".
  function expectVoice(v, lang, fast) {
    v.quiet = { since: Date.now(), lang: lang,
      fallback: fast ? fast.text : ACK[lang] || ACK.en, fallbackId: fast ? fast.id : '' };
    watchQuiet(v);
  }

  function quietTimes(v) {
    var control = v.form.querySelector('.voice-control');
    var attribute = control && control.getAttribute('data-voice-quiet-seconds');
    var given = String(attribute || '').trim().split(/\s+/).map(Number);
    return [(given[0] * 1000) || NARRATE_FIRST_MS, (given[1] * 1000) || NARRATE_EVERY_MS];
  }

  // Times the next quiet spell: 5 s after a send, else 30 s while the agent works,
  // counted from the last thing said. A reading stopped by a tap on 🎧 never ends
  // in `spoke`, so the next quiet spell waits for something else to be said or
  // sent: a limit the owner accepted on #3799.
  function watchQuiet(v) {
    clearTimeout(v.quietTimer);
    if (current() !== v || !v.reads) return;
    var q = v.quiet;
    if (q && !working() && Date.now() - q.since > NARRATE_GIVE_UP_MS) { v.quiet = q = null; }
    if (!q && !working()) return;
    var from = Math.max(v.voicedAt || 0, v.quietFrom || 0, q ? q.since : 0);
    var wait = quietTimes(v)[q ? 0 : 1] - (Date.now() - from);
    v.quietTimer = setTimeout(function () { quietSpell(v); }, Math.max(0, wait));
  }

  // The chat has said nothing for a while: the fast agent says what it is doing.
  function quietSpell(v) {
    if (current() !== v || !v.reads || v.narrating) return;
    var q = v.quiet;
    // Said aloud since the send: the chat answered for itself.
    if (q && (v.voicedAt || 0) > q.since) { v.quiet = null; watchQuiet(v); return; }
    // Its turn has not started yet; the status swap that starts it watches again.
    if (!working()) return;
    // A question box, even one left to answer on screen, or something being said,
    // speaks for the chat already; the box closing watches again.
    if (v.question || questionForm() || v.speaking || v.mode === 'speaking') return;
    if (v.relaying || (v.mode === 'listening' && (heard(v) || v.hearing)) ||
        (v.mode === 'resting' && !v.idled)) {
      v.quietTimer = setTimeout(function () { quietSpell(v); }, SEND_AFTER_MS);
      return;
    }
    var asked = Date.now();
    var id = phraseId(asked);
    var before = v.voicedAt;
    var lang = q ? q.lang : (v.answerLang || v.always || 'en');
    var from = Math.max(v.voicedAt || 0, v.quietFrom || 0, q ? q.since : 0);
    v.narrating = true;
    fetch('/api/voice/narrate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_key: v.key, run: v.run, lang: lang, id: id,
        seconds: Math.round((asked - from) / 1000) })
    }).then(function (reply) { return reply.status === 200 ? reply.json() : null; })
      .catch(function () { return null; })
      .then(function (body) {
        v.narrating = false;
        if (current() !== v) return;
        var said = body && typeof body.say === 'string' ? speakable(body.say) : '';
        // The phrase whose held line is said instead, for the voice log.
        var phrase = '';
        if (!said && q && v.quiet === q) { said = q.fallback; phrase = q.fallbackId; }
        // Nothing new while the work goes on: say so rather than nothing.
        if (!said && !q) said = STILL[lang] || STILL.en;
        if (v.quiet === q) v.quiet = null;
        v.quietFrom = Date.now();
        // The chat spoke, a box opened, the turn ended or a new send came while it
        // was asked.
        if (v.voicedAt !== before || !working() || v.question || questionForm() || v.speaking ||
            (v.quiet && v.quiet !== q) ||
            v.relaying || !said || !sayQuietly(v, said)) watchQuiet(v);
        else {
          reportSpoken(v, id, said);
          if (phrase) reportSpoken(v, phrase, said);
        }
      });
  }

  // The voice log keeps what was really said, not only what was proposed (#3799).
  function reportSpoken(v, id, text) {
    try {
      fetch('/api/voice/sent', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify({ session_key: v.key, id: id, spoken: text })
      }).catch(function () { /* the log then shows it as not spoken */ });
    } catch (e) { /* the log then shows it as not spoken */ }
  }

  // The fast agent's line on the chat's quiet work, said like a 🔊 note: after it
  // the microphone is as it was.
  function sayQuietly(v, text) {
    if (v.mode === 'speaking' || (v.mode === 'resting' && !v.idled)) return false;
    if (v.mode === 'listening' && (heard(v) || v.hearing)) return false;
    var asleep = v.mode === 'waiting';
    var idled = v.mode === 'resting';
    speak(text, function () {
      v.noting = false;
      if (replyIfEnded(v)) return;
      if (working() && idled) rest('Voice mode on', true);
      else listen(!working() ? 'answer' : asleep ? 'wake' : 'turn');
      if (working()) speakNote(v);
    }, spokenLanguage(v, text));
    v.noting = true;
    return true;
  }

  function onSse(event) {
    var target = event.target;
    if (!target || !current()) return;
    if (target.id === 'session-status') onStatus();
    else if (target.classList && target.classList.contains('transcript')) {
      if (voice.replyDeadline && Date.now() < voice.replyDeadline) settleReply(voice);
      else if (working()) speakNote(voice);
      else if (voice.awaiting && unreadReply(voice)) waitForReply(voice);
    } else if (target.id === 'pending-interaction-slot') {
      onInteraction();
    }
  }

  // The question box came or went. Also reached by the page's own refresh of the
  // box, which a reconnected stream does not re-send (#3799); each box is asked once.
  function onInteraction() {
    var form = questionForm();
    if (form) { askQuestions(form); return; }
    if (voice.question) {
      voice.question = null;
      voice.asked = null;
      listen(working() ? 'turn' : 'answer');
    }
    // The work goes on after the box: its quiet spells count again.
    watchQuiet(voice);
  }

  function onSwap(event) {
    var target = event.target;
    if (target && target.id === 'pending-interaction-slot' && current()) onInteraction();
  }

  // ---- on and off ---------------------------------------------------------

  function wakeLock() {
    var v = voice;
    if (!v || !navigator.wakeLock || v.lock || document.hidden) return;
    navigator.wakeLock.request('screen').then(function (lock) {
      if (voice === v) v.lock = lock;
      else lock.release();
    }).catch(function () { /* not allowed here */ });
  }

  // Voice mode is on while 🎧 reads aloud or 🎤 listens (#3540). Whichever is
  // tapped first starts it, inside that tap, which is what lets a phone play and
  // record later; with both off it ends.
  function on(form) {
    var input = form && form.querySelector('textarea[name="message"]');
    var control = form && form.querySelector('.voice-control');
    if (!input || !control) return null;
    // A running dictation would hear "Voice mode on": its own stop keeps what it
    // heard, then voice mode starts.
    stopDictation();
    var audio = spare;
    spare = null;
    try { audio = audio || (Context ? new Context() : null); } catch (e) { audio = null; }
    if (!control.hasAttribute('data-host-speech') || !audio) {
      if (audio && audio.close) audio.close();
      var status = control.querySelector('.speech-status');
      if (status && !audio) setStatus(status, 'Voice mode can’t play audio here');
      return null;
    }
    voice = {
      key: sessionKey(), form: form, input: input,
      button: control.querySelector('[data-voice-button]'),
      mic: form.querySelector('[data-voice-mic]'),
      status: control.querySelector('.speech-status'), line: control.getAttribute('data-voice-line'),
      lineFi: control.getAttribute('data-voice-line-fi'), lang: 'en', acked: [], aloud: [],
      unheard: control.getAttribute('data-voice-unheard'),
      short: control.getAttribute('data-voice-short'),
      // The one language this owner speaks, when set: no guessing it (#3525).
      always: control.getAttribute('data-voice-language') === 'fi' ? 'fi' : '',
      idleMs: (Number(control.getAttribute('data-voice-idle-seconds')) * 1000) || IDLE_LISTEN_MS,
      wake: control.getAttribute('data-voice-wake') || 'whisper',
      audio: audio, mode: 'idle', phrases: [], base: '', working: working(),
      blocked: {}, run: Date.now().toString(36) + Math.random().toString(36).slice(2),
      reads: false, hears: false, room: true
    };
    document.documentElement.classList.add('voice-on');
    wakeLock();
    var v = voice;
    // Who started a turn already running is not known: it counts as unheard (#3520).
    v.unheardTurn = v.working;
    warmFast(v);
    // Only replies that arrive from now on are read, not the one already on screen.
    var shown = lastReply();
    v.spokenId = shown ? shown.id : null;
    return v;
  }

  // 🎧 on: replies and 🔊 notes are read aloud. Speaking inside the tap is what
  // lets iOS speak later without one.
  function startReading(v) {
    v.reads = true;
    remember(v);
    v.button.setAttribute('aria-pressed', 'true');
    v.button.title = 'Reading replies aloud — tap to stop';
    var question = !v.question && questionForm();
    var line = v.arrival || ownLine('on');
    v.arrival = '';
    speak(line, function () {
      if (question) askQuestions(question);
      else if (v.question) listen('question');
      else listen(working() ? 'turn' : 'answer');
    });
  }

  // 🎧 tapped while something is read (#3779): that reading stops, as heard only
  // in part, and 🎧 stays on. What it would have done at its end follows: a reply
  // that landed meanwhile is still read, and listening carries on as 🎤 has it.
  var HUSHED = 'Stopped reading';
  var HUSHED_MS = 2500;

  function hush(v) {
    var noting = v.noting;
    v.noting = false;
    stopSpeaking(v);
    if (v.question) listen('question');
    else if (!(noting && replyIfEnded(v))) listen(working() ? 'turn' : 'answer');
    if (current() !== v) return;
    var after = v.status ? v.status.textContent : '';
    showStatus(HUSHED);
    setTimeout(function () {
      if (current() === v && v.status.textContent === HUSHED) showStatus(after);
    }, HUSHED_MS);
  }

  // 🎧 off: nothing more is read; listening carries on as 🎤 has it.
  function stopReading(v) {
    var speaking = v.mode === 'speaking';
    v.reads = false;
    remember(v);
    v.button.setAttribute('aria-pressed', 'false');
    v.button.title = 'Read replies aloud';
    stopSpeaking(v);
    if (!v.hears && !listeningMode(v) && !v.relaying) { off(''); return; }
    if (!speaking) return;
    if (v.question) listen('question');
    else listen(working() ? 'turn' : 'answer');
  }

  // 🎤 on: it listens until 🎤 is tapped again. Something being read is heard out
  // first, except for a tap to talk now (`v.once`).
  function startHearing(v) {
    if (v.relaying) return;
    v.hears = true;
    remember(v);
    if (v.question) v.question.tapped = true;
    if (!v.captureReady) openMicrophone(v);
    if (v.audio && v.audio.resume) v.audio.resume();
    wakeLock();
    showMic(v);
    if (v.mode === 'speaking') {
      if (!v.once) return;
      stopSpeaking(v);
    }
    if (v.question) { listen('question'); return; }
    // A question box already open when 🎤 started voice mode, which only 🎧 asks
    // otherwise: the tap answers it, rather than sending a chat message.
    var form = questionForm();
    if (form) {
      v.asked = null;
      v.tapQuestion = true;
      askQuestions(form);
      v.tapQuestion = false;
      return;
    }
    listen(working() ? 'turn' : 'answer');
  }

  // 🎤 off: the microphone is let go of and words not yet sent are taken back; it
  // comes back on in Jarvis.
  function stopHearing(v) {
    v.hears = false;
    v.room = true;
    v.once = false;
    remember(v);
    if (listeningMode(v)) {
      restoreDraft(v);
      stopListening(v);
      showStatus('');
    }
    closeMicrophone(v);
    showMic(v);
    tone(330);
    if (!v.reads && v.mode !== 'speaking') off('');
  }

  // Paused (a hidden tab the browser would not serve, dictation, a listener that
  // would not start): a tap on either button goes on, reading what was missed.
  function paused(v) {
    return v.mode === 'resting' && !v.question && !v.idled;
  }

  // `leaving`: the chat it was on in went away, not voice mode: the next chat
  // opened starts it again, on the audio this one played through.
  function off(why, leaving) {
    var v = voice;
    if (!v) return;
    // A jump still waiting goes now when voice mode is turned off, and is dropped when
    // the page left the deck chat some other way (#3816).
    var jump = !leaving && v.jump;
    v.jump = null;
    clearTimeout(v.jumpTimer);
    if (jump) setTimeout(jump.go, 0);
    if (!leaving) forget();
    stopListening(v);
    stopSpeaking(v);
    clearTimeout(v.replyTimer);
    clearTimeout(v.noteTimer);
    clearTimeout(v.quietTimer);
    voice = null;
    // Its fast agent and memory go with it (#3444).
    try {
      fetch('/api/voice/end', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify({ session_key: v.key, run: v.run })
      }).catch(function () { /* it closes itself when idle */ });
    } catch (e) { /* it closes itself when idle */ }
    announce('off');
    v.button.setAttribute('aria-pressed', 'false');
    v.button.removeAttribute('data-voice-state');
    v.button.title = 'Read replies aloud';
    if (v.mic) {
      v.mic.removeAttribute('data-voice-hears');
      v.mic.removeAttribute('data-voice-state');
    }
    if (v.lock) { try { v.lock.release(); } catch (e) { /* gone */ } }
    closeMicrophone(v);
    if (leaving && v.audio && v.audio.state === 'running') {
      if (spare && spare.close) { try { spare.close(); } catch (e) { /* closed */ } }
      spare = v.audio;
    } else if (v.audio && v.audio.close) {
      try { v.audio.close(); } catch (e) { /* closed */ }
    }
    if (v.status) {
      setStatus(v.status, why || '');
      if (why) setTimeout(function () { if (!voice) v.status.hidden = true; }, 4000);
    }
  }

  // ---- into the next chat (#3548) -------------------------------------------

  // Kept for this tab only: a phone app opened again later starts with it off.
  function remember(v) {
    var hears = !!v.hears;
    try {
      if (v.reads || hears) sessionStorage.setItem(CARRY_KEY, JSON.stringify({ reads: !!v.reads, hears: hears, room: !!v.room }));
      else sessionStorage.removeItem(CARRY_KEY);
    } catch (e) { /* this chat only */ }
  }

  function forget() {
    try { sessionStorage.removeItem(CARRY_KEY); } catch (e) { /* not stored */ }
  }

  function carried() {
    var kept = null;
    try { kept = JSON.parse(sessionStorage.getItem(CARRY_KEY) || 'null'); } catch (e) { kept = null; }
    return kept && (kept.reads || kept.hears) ? kept : null;
  }

  // A chat opened with voice mode on in the one left: it starts at once as the
  // taps left it. A browser that will not play without a tap on this page (a
  // full load) pauses it instead, and a tap on 🎧 or 🎤 goes on.
  function carryOn() {
    var kept = !current() && carried();
    var button = kept && document.querySelector('form [data-voice-button]');
    if (!button) return;
    var v = on(button.closest('form'));
    if (!v) return;
    v.carrying = true;
    v.arrival = arrived();
    var hears = !!kept.hears;
    v.room = kept.room !== false;
    function go() {
      if (voice !== v || !v.carrying) return;
      v.carrying = false;
      if (kept.reads) startReading(v);
      if (hears) startHearing(v);
    }
    if (v.audio.state === 'running') { go(); return; }
    setTimeout(function () {
      if (voice !== v || !v.carrying || v.audio.state === 'running') return;
      v.reads = !!kept.reads;
      v.hears = hears;
      if (v.reads) v.button.setAttribute('aria-pressed', 'true');
      v.carrying = false;
      remember(v);
      rest(CARRY_PAUSED);
    }, CARRY_START_MS);
    try { v.audio.resume().then(go, function () { /* the pause above */ }); } catch (e) { /* paused */ }
  }

  // ---- jumping to another chat (#3816) ---------------------------------------

  // The deck chat asked to move this page to another chat: with voice mode on here, it
  // waits until the turn has ended and its reply was read, and `listen` makes it. Said
  // when voice mode is off here: chat_jump.js moves at once.
  window.AgentDeckVoiceJump = function (jump, go) {
    var v = current();
    if (!v) return false;
    clearTimeout(v.jumpTimer);
    v.jump = { title: String(jump.title || ''), go: go };
    v.jumpTimer = setTimeout(function () { if (current() === v) jumpNow(v); }, JUMP_WAIT_MS);
    // The turn already ended and its reply was read: nothing is left to hear here.
    if (!working() && !v.relaying && !v.replyDeadline && v.mode !== 'speaking' &&
        !v.question && !unreadReply(v)) {
      jumpNow(v);
    }
    return true;
  };

  function jumpNow(v) {
    var jump = v.jump;
    if (!jump) return;
    v.jump = null;
    clearTimeout(v.jumpTimer);
    try {
      sessionStorage.setItem(ARRIVE_KEY, JSON.stringify({ title: jump.title, at: Date.now() }));
    } catch (e) { /* said as "Voice mode on." */ }
    jump.go();
  }

  function goTo(path) {
    if (window.AgentDeckChatJump) AgentDeckChatJump.go(path);
    else location.assign(path);
  }

  // The line said on arriving by a jump, once: "Now in: <the chat's title>". One left
  // from a jump that never arrived with voice mode on is old by the next chat opened.
  var ARRIVE_MS = 30000;
  function arrived() {
    var kept = null;
    try {
      kept = JSON.parse(sessionStorage.getItem(ARRIVE_KEY) || 'null');
      sessionStorage.removeItem(ARRIVE_KEY);
    } catch (e) { kept = null; }
    if (!kept || !(Date.now() - kept.at < ARRIVE_MS)) return '';
    var title = speakable(String(kept.title || '').replace(/[^\p{L}\p{N}\s.,:'!?#-]+/gu, ' '))
      .replace(/[\s.!?:,]+$/, '');
    return title ? ownLine('now') + ' ' + title + '.' : '';
  }

  // ---- the old device-kept mode (#3540) ---------------------------------------

  try { localStorage.removeItem(OLD_LISTEN_KEY); } catch (e) { /* not stored here */ }

  // Dictation and voice mode would fight over the microphone: a dictation running
  // when voice mode is about to listen is stopped by its own button, keeping what
  // it heard.
  var releasing = false;
  function stopDictation() {
    var mic = document.querySelector('[data-speech-button][aria-pressed="true"]');
    if (!mic) return;
    releasing = true;
    try { mic.click(); } finally { releasing = false; }
  }

  document.addEventListener('htmx:load', carryOn);

  // ---- the buttons ----------------------------------------------------------

  // A long press on 🎤 opens the menu of how it listens (#3787); the click that
  // follows it is swallowed.
  var LONG_PRESS_MS = 600;
  var pressTimer = null;
  var longPressed = false;
  document.addEventListener('pointerdown', function (event) {
    var mic = event.target.closest && event.target.closest('[data-voice-mic]');
    if (!mic) return;
    longPressed = false;
    clearTimeout(pressTimer);
    pressTimer = setTimeout(function () {
      pressTimer = null;
      longPressed = true;
      openMenu(mic);
    }, LONG_PRESS_MS);
  });
  ['pointerup', 'pointercancel'].forEach(function (type) {
    document.addEventListener(type, function () {
      clearTimeout(pressTimer);
      pressTimer = null;
    }, true);
  });
  // A long press on a phone opens the context menu instead of ending the press.
  document.addEventListener('contextmenu', function (event) {
    if (event.target.closest && event.target.closest('[data-voice-mic]')) event.preventDefault();
  });

  function menuOf(mic) {
    var control = mic.closest('.speech-control');
    return control && control.querySelector('[data-voice-mic-menu]');
  }

  // Which item is ticked: Off while 🎤 is off, else Jarvis or Always.
  function openMenu(mic) {
    var menu = menuOf(mic);
    if (!menu) return;
    var v = current();
    var picked = !(v && v.hears) ? 'off' : v.room ? 'jarvis' : 'open';
    menu.querySelectorAll('[data-voice-mic-mode]').forEach(function (item) {
      item.setAttribute('aria-checked', item.getAttribute('data-voice-mic-mode') === picked ? 'true' : 'false');
    });
    // It hangs from 🎤's right edge; on a crowded phone composer 🎤 sits far left, so
    // it slides right until it is back on the screen, as the status bubble does.
    menu.style.right = '';
    menu.hidden = false;
    var left = menu.getBoundingClientRect().left;
    if (left < STATUS_MARGIN) menu.style.right = (left - STATUS_MARGIN) + 'px';
  }

  function closeMenus() {
    document.querySelectorAll('[data-voice-mic-menu]').forEach(function (menu) { menu.hidden = true; });
  }

  // A menu item: Off lets go of the microphone; Jarvis or Always turns 🎤 on that
  // way, or switches it while it is on.
  function pickListening(form, mode) {
    var v = current();
    if (mode === 'off') {
      if (v && (v.hears || listeningMode(v))) stopHearing(v);
      return;
    }
    if (!v) v = on(form);
    if (!v) return;
    v.carrying = false;
    var room = mode === 'jarvis';
    if (!v.hears) {
      v.room = room;
      startHearing(v);
      return;
    }
    // Always picked while it waits after the idle spell listens again at once.
    if (v.room === room && (room || v.mode !== 'waiting')) return;
    v.room = room;
    remember(v);
    tone(room ? 330 : 880);
    if (v.mode === 'speaking' || v.relaying) return;
    restoreDraft(v);
    listen(v.question ? 'question' : working() ? 'turn' : 'answer');
  }

  // 🎤 in voice mode's chat composer. Captured before composer_speech.js sees it,
  // which dictates only in the other forms (#3787). A tap is "talk now": from off,
  // while it waits for "Hey Jarvis" or while it reads, it listens for one message.
  // While it listens, a tap sends what was heard and goes to Jarvis.
  document.addEventListener('click', function (event) {
    var item = event.target.closest && event.target.closest('[data-voice-mic-mode]');
    if (item) {
      event.preventDefault();
      event.stopPropagation();
      closeMenus();
      pickListening(item.closest('form'), item.getAttribute('data-voice-mic-mode'));
      return;
    }
    var mic = event.target.closest && event.target.closest('[data-voice-mic]');
    if (!mic) {
      if (!(event.target.closest && event.target.closest('[data-voice-mic-menu]'))) closeMenus();
      return;
    }
    if (releasing) return;
    event.preventDefault();
    event.stopPropagation();
    if (longPressed) { longPressed = false; return; }
    closeMenus();
    var v = current();
    if (v) v.carrying = false;
    if (!v) v = on(mic.closest('form'));
    if (!v) return;
    if (paused(v)) { resumeFromPause(v); return; }
    if (!v.hears) {
      v.room = true;
      v.once = true;
      startHearing(v);
      return;
    }
    if (v.mode === 'listening') {
      v.room = true;
      remember(v);
      tone(330);
      clearTimeout(v.sendTimer);
      clearTimeout(v.holdTimer);
      // A phrase still being said or transcribed is sent once its words are back.
      var rec = v.rec;
      if (rec && !rec.aborted && (rec.done || rec.cut())) { v.doneTap = true; return; }
      if (heard(v)) { finish(v); return; }
      listen(v.question ? 'question' : working() ? 'turn' : 'answer');
      return;
    }
    if (v.mode === 'speaking') stopSpeaking(v);
    v.once = true;
    listen(v.question ? 'question' : working() ? 'turn' : 'answer');
  }, true);

  // 🎧 turns reading aloud on and off; while something is read, a tap only stops it.
  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[data-voice-button]');
    if (!button) return;
    event.preventDefault();
    var v = current();
    if (v) v.carrying = false;
    if (!v) v = on(button.closest('form'));
    if (!v) return;
    if (paused(v)) { resumeFromPause(v); return; }
    if (v.reads && v.speaking) hush(v);
    else if (v.reads) stopReading(v);
    else startReading(v);
  });

  function resumeFromPause(v) {
    if (v.audio && v.audio.resume) v.audio.resume();
    wakeLock();
    if (v.hears && !v.captureReady) openMicrophone(v);
    resume(v);
  }

  // Keep the mobile keyboard where it is, as the 🎤 button does.
  document.addEventListener('mousedown', function (event) {
    if (event.target.closest && event.target.closest('[data-voice-button], [data-voice-mic-mode]')) {
      event.preventDefault();
    }
  });

  // The instruction line rides on every send while voice mode is on. Capture on
  // `document` runs before interaction_feedback.js copies the form for a retry.
  // A slash command is left alone: anything after it would become its argument.
  document.addEventListener('htmx:configRequest', function (event) {
    var v = current();
    var detail = event.detail;
    if (!v || !detail || detail.elt !== v.form) return;
    v.sentAt = Date.now();
    v.awaiting = true;
    // A send of words just heard, with what was unheard when they ended (#3520).
    var spoken = v.spoken && Date.now() - v.spoken.at < 2000 ? v.spoken : null;
    v.spoken = null;
    if (!v.line) return;
    var params = detail.parameters;
    var message = params && params.message;
    if (typeof message !== 'string' || !message.trim() || /^\s*\//.test(message)) return;
    v.answerLang = (v.always || wordsLanguage(message) || v.lang) === 'fi' && v.lineFi ? 'fi' : 'en';
    detail.voiceAck = v.answerLang;
    // The 🔊 line and what went unheard matter only while 🎧 reads aloud (#3540).
    var lines = [];
    if (v.reads && v.unheard && ((spoken && spoken.unheard) || unheardNow(v))) {
      var heardLast = spoken && spoken.unheard ? spoken.last : v.lastAloud;
      var quoted = heardLast ? '“' + heardLast + '”' : 'nothing yet';
      lines.push(v.unheard.replace('{heard}', function () { return quoted; }));
      // A finished turn's reply still unread is not read after this send (#3771):
      // the agent is told the person did not hear it and says again what they need,
      // so reading it too would say it twice. It stays on screen.
      if (!working() && unreadReply(v)) v.spokenId = lastReply().id;
    }
    if (v.short && spoken && shortAnswer(message)) lines.push(v.short);
    if (v.reads) lines.push(v.answerLang === 'fi' ? v.lineFi : v.line);
    if (!lines.length) return;
    var line = lines.join('\n\n');
    params.message = message.replace(/\s+$/, '') + '\n\n' + line;
  }, true);

  // Its action ID is only on the request once action_timing.js has run, on `body`.
  // Each send is acknowledged once, as soon as it goes; a retry reuses its ID.
  document.addEventListener('htmx:configRequest', function (event) {
    var v = current();
    var detail = event.detail;
    var id = detail && detail.voiceAck && detail.parameters && detail.parameters.client_action_id;
    if (!v || !id || v.acked.indexOf(id) >= 0) return;
    v.acked.push(id);
    if (v.acked.length > 20) v.acked.shift();
    var lang = detail.voiceAck;
    expectVoice(v, lang, v.fastSay);
  });

  document.addEventListener('htmx:sseMessage', onSse, true);
  document.addEventListener('htmx:afterSwap', onSwap, true);

  // Opening another chat in place ends voice mode there: its microphone, wake lock
  // and replies belong to the chat it was turned on in. It then starts again in the
  // chat opened (#3548). The same event also marks a column mounting beside this
  // chat on a desktop, which leaves it on (#3408).
  document.addEventListener('agentdeck:session-navigation', function () {
    if (voice) current();
    carryOn();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', carryOn);
  else carryOn();

  // Hidden, it carries on; what the browser will not do there pauses it then.
  document.addEventListener('visibilitychange', function () {
    var v = current();
    if (!v) return;
    if (document.hidden) {
      v.lock = null;  // the browser releases it with the page hidden
      // The sentence being spoken may have just begun: give it its whole time.
      if (v.speaking) { progressed(v, v.sentenceChars || 0); watchSpeech(v); }
    } else {
      wakeLock();
    }
  });

  // A phone stops a background tab's scripts altogether: nothing ran meanwhile, so
  // say so when it wakes rather than claim to be listening.
  document.addEventListener('freeze', function () { if (voice) voice.frozen = true; });
  document.addEventListener('resume', function () {
    var v = current();
    if (!v || !v.frozen) return;
    v.frozen = false;
    background(v, 'frozen');
  });
})();
