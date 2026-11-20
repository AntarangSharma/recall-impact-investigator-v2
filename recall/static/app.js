for (const form of document.querySelectorAll("form[data-loading]")) {
  form.addEventListener("submit", () => {
    const button = form.querySelector("button");
    if (button) {
      button.textContent = form.dataset.loading;
      button.disabled = true;
      form.setAttribute("aria-busy", "true");
    }
  });
}
