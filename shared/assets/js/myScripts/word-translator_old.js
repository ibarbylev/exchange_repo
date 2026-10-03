/**
 * Клик по болгарскому слову внутри [data-translatable].
 * Подключается один раз на странице Classroom.
 * Запрос: /{source}/{ui}/api/translator/{word}
 * Аудио: /media/audio/{path}.mp3
 */
(function (window, document) {
    'use strict';

    var WORD_RE = /[\u0400-\u04FF]+/g;
    var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1, INPUT: 1, BUTTON: 1, AUDIO: 1 };

    var WordTranslator = {
        cssHref: '/static/css/my-css/word-translator.css',
        mediaPrefix: '/media/audio/',
        cache: Object.create(null),
        abort: null,
        audio: null,
        started: false
    };

    function langs() {
        var box = document.getElementById('classroomContent');
        return {
            source: (box && box.dataset.sourceLang) || 'bg',
            ui: (box && box.dataset.uiLang) || 'ru'
        };
    }

    function translatorUrl(word) {
        var pair = langs();
        return '/' + pair.source + '/' + pair.ui + '/api/translator/' + encodeURIComponent(word);
    }

    function audioUrl(audioPath) {
        if (!audioPath) return '';
        var clean = String(audioPath).replace(/^\/+/, '').replace(/\.mp3$/i, '');
        return WordTranslator.mediaPrefix + clean.split('/').map(encodeURIComponent).join('/') + '.mp3';
    }

    function loadCss() {
        if (document.getElementById('word-translator-css')) return;
        if (document.querySelector('link[href="' + WordTranslator.cssHref + '"]')) return;
        var link = document.createElement('link');
        link.id = 'word-translator-css';
        link.rel = 'stylesheet';
        link.href = WordTranslator.cssHref;
        document.head.appendChild(link);
    }

    function ensurePopup() {
        var popup = document.getElementById('wordTranslatorPopup');
        if (popup) return popup;
        popup = document.createElement('div');
        popup.id = 'wordTranslatorPopup';
        popup.className = 'word-translator-popup';
        popup.innerHTML =
            '<div class="wt-translation"></div>' +
            '<button type="button" class="wt-audio"><i class="fas fa-volume-up"></i></button>';
        document.body.appendChild(popup);
        popup.addEventListener('click', function (event) {
            var btn = event.target.closest('.wt-audio');
            if (btn && btn.dataset.audio) {
                event.stopPropagation();
                playAudio(btn.dataset.audio);
            }
        });
        return popup;
    }

    function setPopup(state) {
        var popup = ensurePopup();
        var wordEl = popup.querySelector('.wt-word');
        var textEl = popup.querySelector('.wt-translation');
        var btn = popup.querySelector('.wt-audio');
        if (wordEl) wordEl.textContent = state.word || '';
        if (textEl) {
            textEl.textContent = state.text || '';
            textEl.classList.toggle('is-muted', Boolean(state.muted));
        }
        if (btn) {
            btn.classList.toggle('is-visible', Boolean(state.audio));
            btn.dataset.audio = state.audio || '';
        }
        popup.classList.add('is-visible');
    }

    function hidePopup() {
        var popup = document.getElementById('wordTranslatorPopup');
        if (popup) popup.classList.remove('is-visible');
        document.querySelectorAll('.js-bg-word.is-active').forEach(function (el) {
            el.classList.remove('is-active');
        });
        if (WordTranslator.abort) {
            WordTranslator.abort.abort();
            WordTranslator.abort = null;
        }
    }

    function placePopup(span) {
        var popup = ensurePopup();
        popup.classList.add('is-visible');
        var rect = span.getBoundingClientRect();
        var box = popup.getBoundingClientRect();
        var left = rect.left + rect.width / 2 - box.width / 2;
        var top = rect.top - box.height - 8;
        if (left < 8) left = 8;
        if (left + box.width > window.innerWidth - 8) {
            left = window.innerWidth - box.width - 8;
        }
        if (top < 8) top = rect.bottom + 8;
        popup.style.left = left + 'px';
        popup.style.top = top + 'px';
    }

    function playAudio(path) {
        var url = audioUrl(path);
        if (!url) return;
        if (WordTranslator.audio) {
            WordTranslator.audio.pause();
            WordTranslator.audio = null;
        }
        var audio = new Audio(url);
        WordTranslator.audio = audio;
        audio.play().catch(function () {});
    }

    function shouldSkip(el) {
        if (!el || el.nodeType !== 1) return false;
        if (SKIP_TAGS[el.tagName]) return true;
        if (el.classList && el.classList.contains('js-bg-word')) return true;
        if (el.hasAttribute && el.hasAttribute('data-checkbox')) return true;
        if (el.closest && el.closest('[data-checkbox]')) return true;
        return false;
    }

    function wrapTextNode(node) {
        var text = node.nodeValue;
        WORD_RE.lastIndex = 0;
        if (!text || !WORD_RE.test(text)) return;
        WORD_RE.lastIndex = 0;
        var frag = document.createDocumentFragment();
        var last = 0;
        var match;
        while ((match = WORD_RE.exec(text))) {
            if (match.index > last) {
                frag.appendChild(document.createTextNode(text.slice(last, match.index)));
            }
            var span = document.createElement('span');
            span.className = 'js-bg-word';
            span.dataset.word = match[0].toLowerCase();
            span.textContent = match[0];
            frag.appendChild(span);
            last = match.index + match[0].length;
        }
        if (last < text.length) {
            frag.appendChild(document.createTextNode(text.slice(last)));
        }
        node.parentNode.replaceChild(frag, node);
    }

    function wrapBlock(block) {
        var walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
            acceptNode: function (node) {
                if (!node.nodeValue || !/[\u0400-\u04FF]/.test(node.nodeValue)) {
                    return NodeFilter.FILTER_REJECT;
                }
                var parent = node.parentElement;
                if (!parent || shouldSkip(parent)) return NodeFilter.FILTER_REJECT;
                return NodeFilter.FILTER_ACCEPT;
            }
        });
        var nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        nodes.forEach(wrapTextNode);
    }

    function process(scope) {
        if (!scope) return;
        if (scope.nodeType === 1 && scope.hasAttribute && scope.hasAttribute('data-translatable')) {
            wrapBlock(scope);
        }
        if (!scope.querySelectorAll) return;
        scope.querySelectorAll('[data-translatable]').forEach(wrapBlock);
    }

    function showTranslation(word, span) {
        var cached = WordTranslator.cache[word];
        if (cached) {
            setPopup({
                word: cached.word || word,
                text: (cached.translation || []).join(', ') || 'перевод не найден',
                muted: !(cached.translation && cached.translation.length),
                audio: cached.audio || ''
            });
            placePopup(span);
            return;
        }

        setPopup({ word: word, text: 'загрузка…', muted: true, audio: '' });
        placePopup(span);

        if (WordTranslator.abort) WordTranslator.abort.abort();
        WordTranslator.abort = new AbortController();

        fetch(translatorUrl(word), {
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' },
            signal: WordTranslator.abort.signal
        }).then(function (res) {
            if (!res.ok) throw new Error(String(res.status));
            return res.json();
        }).then(function (data) {
            var payload = {
                word: data.word || word,
                translation: Array.isArray(data.translation) ? data.translation : [],
                audio: data.audio || ''
            };
            WordTranslator.cache[word] = payload;
            if (!span.classList.contains('is-active')) return;
            setPopup({
                word: payload.word,
                text: payload.translation.join(', ') || 'перевод не найден',
                muted: !payload.translation.length,
                audio: payload.audio
            });
            placePopup(span);
        }).catch(function (err) {
            if (err && err.name === 'AbortError') return;
            setPopup({
                word: word,
                text: 'не удалось получить перевод',
                muted: true,
                audio: ''
            });
            placePopup(span);
        });
    }

    function onDocClick(event) {
        var popup = document.getElementById('wordTranslatorPopup');
        var span = event.target.closest && event.target.closest('.js-bg-word');
        if (span) {
            document.querySelectorAll('.js-bg-word.is-active').forEach(function (el) {
                el.classList.remove('is-active');
            });
            span.classList.add('is-active');
            showTranslation(span.dataset.word || span.textContent.toLowerCase(), span);
            return;
        }
        if (popup && popup.contains(event.target)) return;
        hidePopup();
    }

    function start() {
        if (WordTranslator.started) return;
        WordTranslator.started = true;
        loadCss();
        ensurePopup();
        document.addEventListener('click', onDocClick);
        var root = document.getElementById('classroomContent') || document.body;
        process(root);
        var observer = new MutationObserver(function (mutations) {
            mutations.forEach(function (mutation) {
                Array.prototype.forEach.call(mutation.addedNodes, function (node) {
                    if (node.nodeType === 1) process(node);
                });
            });
        });
        observer.observe(root, { childList: true, subtree: true });
    }

    WordTranslator.start = start;
    window.WordTranslator = WordTranslator;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})(window, document);
