document.querySelectorAll('[data-copy], [data-copy-text]').forEach((button) => {
  button.addEventListener('click', async () => {
    const value = button.dataset.copyText || document.querySelector(button.dataset.copy)?.textContent;
    if (!value) return;
    await navigator.clipboard.writeText(value.trim());
    const label = button.textContent;
    button.textContent = '已复制';
    setTimeout(() => { button.textContent = label; }, 1200);
  });
});
