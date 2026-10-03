// Обрабатывает клик по динамку в словаре

function playAudio(filename) {
    if (!filename) {
        console.error("Имя файла не передано");
        return;
    }

    let audioPath = filename.trim();

    // Автоматически добавляем /media/ и .mp3
    if (!audioPath.startsWith('/media/')) {
        audioPath = '/media/audio/' + audioPath;
    }
    if (!audioPath.endsWith('.mp3')) {
        audioPath += '.mp3';
    }

    // console.log("Попытка воспроизведения:", audioPath);

    const audio = new Audio(audioPath);

    audio.play().then(() => {
        // console.log("✅ Воспроизведение начато");
    }).catch(err => {
        console.error("❌ Ошибка воспроизведения:", err);
        alert("Не удалось воспроизвести звук.\nПуть: " + audioPath);
    });
}
