// ===============================
// Translator Language Selector
// ===============================

document.addEventListener("DOMContentLoaded", function () {

    // --- элементы интерфейса ---
    const leftBtn = document.getElementById("left-btn");
    const rightBtn = document.getElementById("right-btn");
    const leftMenu = document.getElementById("left-menu");
    const rightMenu = document.getElementById("right-menu");
    const swapBtn = document.getElementById("swap-langs");

    const form = document.getElementById("word-form");
    const input = document.getElementById("word-input");

    const languages = ["EN","RU","BG","PL"];

    // ===============================
    // Read data from URL
    // ===============================

    const path = window.location.pathname.split("/").filter(Boolean);

    let sourceLang = "EN";
    let uiLang = "BG";

    if (path.length >= 3 && path[2] === "translator") {

        sourceLang = path[0].toUpperCase();
        uiLang = path[1].toUpperCase();

        if (path.length > 3) {
            const word = decodeURIComponent(path.slice(3).join("/"));
            input.value = word;
        }
    }

    leftBtn.textContent = sourceLang;
    rightBtn.textContent = uiLang;

    // ===============================
    // Refresh dropdown
    // ===============================

    function updateMenuOptions() {

        const leftLang = leftBtn.textContent.trim();
        const rightLang = rightBtn.textContent.trim();

        leftMenu.querySelectorAll("a").forEach(a => {
            a.style.display =
                a.textContent.trim() === rightLang ? "none" : "block";
        });

        rightMenu.querySelectorAll("a").forEach(a => {
            a.style.display =
                a.textContent.trim() === leftLang ? "none" : "block";
        });

    }

    updateMenuOptions();

    // ===============================
    // Changing language pair
    // ===============================

    function goToLangPair(newLeft, newRight) {

        const url = `/${newLeft.toLowerCase()}/${newRight.toLowerCase()}/translator/`;

        window.location.href = url;

    }

    // ===============================
    // Left dropdown
    // ===============================

    leftMenu.addEventListener("click", function(e) {

        const link = e.target.closest("a");
        if (!link) return;

        e.preventDefault();

        const newLeft = link.textContent.trim();
        const newRight = rightBtn.textContent.trim();

        goToLangPair(newLeft, newRight);

    });

    // ===============================
    // Right dropdown
    // ===============================

    rightMenu.addEventListener("click", function(e) {

        const link = e.target.closest("a");
        if (!link) return;

        e.preventDefault();

        const newLeft = leftBtn.textContent.trim();
        const newRight = link.textContent.trim();

        goToLangPair(newLeft, newRight);

    });

    // ===============================
    // Swap languages
    // ===============================

    swapBtn.addEventListener("click", function() {

        const newLeft = rightBtn.textContent.trim();
        const newRight = leftBtn.textContent.trim();

        goToLangPair(newLeft, newRight);

    });

    // ===============================
    // Send language form
    // ===============================

    form.addEventListener("submit", function(e) {

        e.preventDefault();

        const word = input.value.trim();
        if (!word) return;

        const left = leftBtn.textContent.trim().toLowerCase();
        const right = rightBtn.textContent.trim().toLowerCase();

        const url = `/${left}/${right}/translator/${encodeURIComponent(word)}`;

        window.location.href = url;

    });

});

