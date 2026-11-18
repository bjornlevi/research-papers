(() => {
  const key = 'research-library-appearance';
  const root = document.documentElement;
  const themes = ['system', 'light', 'dark'];
  const contrasts = ['system', 'more'];
  let preferences = { theme: 'system', contrast: 'system' };
  let storageError = false;

  function apply() {
    root.dataset.theme = preferences.theme;
    root.dataset.contrast = preferences.contrast;
  }

  try {
    const stored = JSON.parse(localStorage.getItem(key) || 'null');
    if (stored !== null) {
      if (!themes.includes(stored.theme) || !contrasts.includes(stored.contrast)) {
        throw new Error('Unrecognized appearance settings.');
      }
      preferences = { theme: stored.theme, contrast: stored.contrast };
    }
  } catch (error) {
    storageError = true;
    console.warn('Appearance settings could not be restored; using device settings.', error);
  }
  apply();

  document.addEventListener('DOMContentLoaded', () => {
    const theme = document.querySelector('#appearance-theme');
    const contrast = document.querySelector('#appearance-contrast');
    const status = document.querySelector('#appearance-status');
    theme.value = preferences.theme;
    contrast.value = preferences.contrast;
    if (storageError) status.textContent = 'Saved appearance could not be restored. Device settings are in use.';

    function update() {
      preferences = { theme: theme.value, contrast: contrast.value };
      apply();
      try {
        localStorage.setItem(key, JSON.stringify(preferences));
        status.textContent = 'Appearance saved in this browser.';
      } catch (error) {
        console.warn('Appearance settings could not be saved.', error);
        status.textContent = 'Appearance changed for this page only. Browser storage is unavailable.';
      }
    }
    theme.addEventListener('change', update);
    contrast.addEventListener('change', update);
    theme.disabled = false;
    contrast.disabled = false;
  });
})();
