(() => {
    const renderAttachmentList = (form, entries) => {
        const list = form.querySelector('.selected-files');
        if (!list) return;
        list.replaceChildren(...entries.flatMap(entry => (
            [...entry.input.files].map(file => {
                const row = document.createElement('span');
                const name = document.createElement('b');
                const size = document.createElement('small');
                name.textContent = `📎 ${file.name}`;
                size.textContent = `${(file.size / 1024 / 1024).toFixed(2)} МБ`;
                row.append(name, size);
                return row;
            })
        )));
    };

    document.querySelectorAll('.attachment-form').forEach(form => {
        const firstInput = form.querySelector('input[type="file"]');
        if (!firstInput) return;
        const entries = [];
        const attachInput = input => {
            input.addEventListener('change', () => {
                if (!input.files?.length) return;
                entries.push({input});
                input.classList.add('attachment-input-stored');
                const nextInput = input.cloneNode();
                nextInput.value = '';
                nextInput.removeAttribute('id');
                input.insertAdjacentElement('afterend', nextInput);
                attachInput(nextInput);
                renderAttachmentList(form, entries);
            }, {once: true});
        };
        attachInput(firstInput);
    });
})();
