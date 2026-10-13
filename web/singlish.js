// Singlish -> Sinhala typing (like Helakuru's phonetic keyboard), for CG text boxes.
// Type in English letters; each word turns into Sinhala when you press space, Enter or a punctuation key.
//   ammaa -> අම්මා   suba upandinayak -> සුබ උපන්දිනයක්   dilshan -> ඩිල්ශන්   dhilshan -> දිල්ශන්
// Rules: d = ඩ, dh = ද, t = ට, th = ත, n = න, N = ණ, l = ල, L = ළ, sh = ශ, Sh = ෂ, x = ං, H = ඃ,
// aa = ා, ae / A = ැ, aee / Aa = ෑ, ii / ee... see the tables below. A word in \backslash stays English.
(function () {
  // consonants, longest first
  const C = [
    ['nndh', 'ඳ'], ['nnd', 'ඬ'], ['nng', 'ඟ'], ['mmb', 'ඹ'], ['GN', 'ඥ'], ['KN', 'ඤ'],
    ['Th', 'ථ'], ['Dh', 'ධ'], ['Ch', 'ඡ'], ['Sh', 'ෂ'], ['Bh', 'භ'],
    ['kh', 'ඛ'], ['gh', 'ඝ'], ['ch', 'ච'], ['jh', 'ඣ'], ['th', 'ත'], ['dh', 'ද'], ['ph', 'ඵ'], ['bh', 'භ'], ['sh', 'ශ'],
    ['k', 'ක'], ['K', 'ඛ'], ['g', 'ග'], ['G', 'ඝ'], ['c', 'ච'], ['C', 'ඡ'], ['j', 'ජ'], ['J', 'ඣ'],
    ['t', 'ට'], ['T', 'ඨ'], ['d', 'ඩ'], ['D', 'ඪ'], ['N', 'ණ'], ['n', 'න'], ['p', 'ප'], ['P', 'ඵ'],
    ['b', 'බ'], ['B', 'ඹ'], ['m', 'ම'], ['y', 'ය'], ['Y', 'ය'], ['r', 'ර'], ['R', 'ර'], ['l', 'ල'], ['L', 'ළ'],
    ['v', 'ව'], ['w', 'ව'], ['W', 'ව'], ['V', 'ව'], ['s', 'ස'], ['S', 'ශ'], ['h', 'හ'], ['f', 'ෆ'], ['F', 'ෆ'], ['z', 'ස'], ['q', 'ක'],
  ];
  // vowel signs after a consonant (no vowel = hal kirima ්), longest first
  const SIGN = [
    ['ai', 'ෛ'], ['au', 'ෞ'], ['aee', 'ෑ'], ['Aa', 'ෑ'], ['aa', 'ා'], ['ae', 'ැ'], ['A', 'ැ'], ['a', ''],
    ['ii', 'ී'], ['ie', 'ී'], ['i', 'ි'], ['I', 'ී'],
    ['uu', 'ූ'], ['u', 'ු'], ['U', 'ූ'],
    ['ee', 'ේ'], ['ea', 'ේ'], ['ei', 'ේ'], ['e', 'ෙ'], ['E', 'ේ'],
    ['oo', 'ෝ'], ['oe', 'ෝ'], ['o', 'ො'], ['O', 'ෝ'],
  ];
  // vowels on their own (start of a word / after a vowel)
  const V = [
    ['ai', 'ඓ'], ['au', 'ඖ'], ['aee', 'ඈ'], ['Aa', 'ඈ'], ['aa', 'ආ'], ['ae', 'ඇ'], ['A', 'ඇ'], ['a', 'අ'],
    ['ii', 'ඊ'], ['i', 'ඉ'], ['I', 'ඊ'], ['uu', 'ඌ'], ['u', 'උ'], ['U', 'ඌ'],
    ['ee', 'ඒ'], ['e', 'එ'], ['E', 'ඒ'], ['oo', 'ඕ'], ['o', 'ඔ'], ['O', 'ඕ'],
  ];
  const HAL = '්', ZWJ = '‍';
  const take = (s, i, table) => { for (const [k, v] of table) if (s.startsWith(k, i)) return [k, v]; return null; };

  function word(s) {
    let out = '', i = 0;
    while (i < s.length) {
      if (s[i] === 'x') { out += 'ං'; i++; continue; }
      if (s[i] === 'H') { out += 'ඃ'; i++; continue; }
      // "ng" at the end of a word or before another consonant: anusvara (ganga -> ගංගා is "gaxgaa" or "ganga")
      if (s.startsWith('ng', i) && (i + 2 === s.length || (take(s, i + 2, C) && !take(s, i + 2, V)))) { out += 'ං'; i += 2; continue; }
      const c = take(s, i, C);
      if (c) {
        i += c[0].length;
        let cons = c[1];
        // r / y straight after a consonant and before a vowel: rakaransaya / yansaya (kra -> ක්‍ර, kya -> ක්‍ය)
        if ((s[i] === 'r' || s[i] === 'y') && s[i] !== c[0] && take(s, i + 1, SIGN) && i + 1 < s.length && /[aeiouAEIOU]/.test(s[i + 1])) {
          cons += HAL + ZWJ + (s[i] === 'r' ? 'ර' : 'ය');
          i++;
        }
        const v = take(s, i, SIGN);
        if (v) { out += cons + v[1]; i += v[0].length; }
        else out += cons + HAL;
        continue;
      }
      const v = take(s, i, V);
      if (v) { out += v[1]; i += v[0].length; continue; }
      out += s[i]; i++;
    }
    return out;
  }

  // whole text: Latin words become Sinhala, \words and numbers stay as typed
  function convert(text) {
    return text.replace(/(\{?)(\\?[A-Za-z]+)/g, (all, br, w) => br ? all : w[0] === '\\' ? w.slice(1) : word(w));
  }

  function on() { try { return localStorage.getItem('singlish') === '1'; } catch (e) { return false; } }
  function setOn(v) { try { localStorage.setItem('singlish', v ? '1' : '0'); } catch (e) {} paintButtons(); }
  function paintButtons() {
    document.querySelectorAll('.sl-toggle').forEach(b => {
      b.classList.toggle('on', on());
      b.title = on() ? 'Singlish typing is ON: English letters turn into Sinhala (press to turn off)'
                     : 'Singlish typing: type Sinhala with English letters (amma -> අම්මා)';
    });
  }

  // a word turns into Sinhala as soon as a space / Enter / punctuation follows it
  function onInput(e) {
    if (!on()) return;
    const el = e.target;
    if (!el.matches || !el.matches('input.sl, textarea.sl')) return;
    if (e.inputType && !e.inputType.startsWith('insert')) return;
    const pos = el.selectionStart, v = el.value;
    const before = v.slice(0, pos);
    const m = before.match(/(\\?[A-Za-z]+)([^A-Za-z\\]+)$/);
    if (!m || m[2].length !== 1 || /[0-9]/.test(m[2])) return;
    const start = before.length - m[0].length;
    if (before[start - 1] === '{') return;          // {t} in a countdown stays as it is
    const conv = m[1][0] === '\\' ? m[1].slice(1) : word(m[1]);
    el.value = v.slice(0, start) + conv + m[2] + v.slice(pos);
    const np = start + conv.length + m[2].length;
    el.setSelectionRange(np, np);
    el.dispatchEvent(new Event('input', {bubbles: true}));
  }
  // leaving the box: the last word turns too
  function onBlur(e) { finish(e.target); }
  function finish(el) {
    if (!on() || !el || !el.matches || !el.matches('input.sl, textarea.sl')) return;
    const v = el.value.replace(/(^|[^{A-Za-z\\])(\\?[A-Za-z]+)$/, (all, pre, w) => pre + (w[0] === '\\' ? w.slice(1) : word(w)));
    if (v !== el.value) { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }
  }
  document.addEventListener('input', onInput, true);
  document.addEventListener('blur', onBlur, true);
  document.addEventListener('DOMContentLoaded', paintButtons);

  window.Singlish = {convert, word, on, finish, toggle: () => setOn(!on()), paint: paintButtons};
})();
