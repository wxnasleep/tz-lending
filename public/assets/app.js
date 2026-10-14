(() => {
  const form = document.querySelector('#lead-form');
  const phone = form.elements.phone;
  const message = document.querySelector('#form-message');
  const submit = form.querySelector('button[type="submit"]');
  const label = submit.querySelector('.submit-label');
  const params = new URLSearchParams(location.search);
  const attribution = {
    utm_source: params.get('utm_source') || '',
    utm_medium: params.get('utm_medium') || '',
    utm_campaign: params.get('utm_campaign') || '',
    utm_content: params.get('utm_content') || '',
    utm_term: params.get('utm_term') || '',
    page: location.href,
    referrer: document.referrer || ''
  };

  const digits = value => value.replace(/\D/g, '');
  const normalizePhone = value => {
    let number = digits(value);
    if (number.length === 9) number = `375${number}`;
    else if (number.length === 10 && number.startsWith('0')) number = `375${number.slice(1)}`;
    return /^375(?:29|33|44|25|17)\d{7}$/.test(number) ? `+${number}` : null;
  };

  const interestTrigger = document.querySelector('#interest-trigger');
  const interestMenu = document.querySelector('#interest-menu');
  const interestCurrent = document.querySelector('#interest-current');
  const interestOptions = [...document.querySelectorAll('.interest-option')];
  const interestChoices = {
    subscription: 'Подписка — 2 пачки раз в месяц',
    order: 'Разовый заказ',
    question: 'Пока есть вопрос'
  };
  const closeInterestMenu = (returnFocus = false) => {
    interestMenu.hidden = true;
    interestTrigger.setAttribute('aria-expanded', 'false');
    interestMenu.parentElement.classList.remove('open');
    if (returnFocus) interestTrigger.focus();
  };
  const openInterestMenu = () => {
    interestMenu.hidden = false;
    interestTrigger.setAttribute('aria-expanded', 'true');
    interestMenu.parentElement.classList.add('open');
  };
  const setInterest = value => {
    const option = interestOptions.find(item => item.dataset.value === value);
    if (!option) return;
    form.elements.interest.value = value;
    interestCurrent.textContent = interestChoices[value];
    interestOptions.forEach(item => {
      const selected = item === option;
      item.classList.toggle('selected', selected);
      item.setAttribute('aria-selected', String(selected));
    });
    closeInterestMenu();
  };

  interestTrigger.addEventListener('click', () => {
    if (interestMenu.hidden) openInterestMenu();
    else closeInterestMenu();
  });
  interestTrigger.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      openInterestMenu();
      const selected = interestOptions.findIndex(option => option.getAttribute('aria-selected') === 'true');
      interestOptions[(selected + (event.key === 'ArrowDown' ? 1 : interestOptions.length - 1)) % interestOptions.length].focus();
    }
  });
  interestOptions.forEach((option, index) => {
    option.addEventListener('click', () => {
      setInterest(option.dataset.value);
      interestTrigger.focus();
    });
    option.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeInterestMenu(true);
      } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const offset = event.key === 'ArrowDown' ? 1 : -1;
        interestOptions[(index + offset + interestOptions.length) % interestOptions.length].focus();
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault();
        interestOptions[event.key === 'Home' ? 0 : interestOptions.length - 1].focus();
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        setInterest(option.dataset.value);
        interestTrigger.focus();
      }
    });
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('.interest-field')) closeInterestMenu();
  });
  form.addEventListener('reset', () => window.setTimeout(() => setInterest('subscription'), 0));

  const mapFrame = document.querySelector('.map-frame iframe');
  const mapOpen = document.querySelector('.map-open');
  const mapChoices = document.querySelectorAll('[data-map-choice]');
  const maps = {
    yandex: {
      title: 'Яндекс Карты: Минск, улица Короля, 9',
      embed: 'https://yandex.ru/map-widget/v1/?ll=27.542023%2C53.902241&z=16&mode=whatshere&whatshere%5Bpoint%5D=27.542023%2C53.902241&whatshere%5Bzoom%5D=17&lang=ru_RU',
      link: 'https://yandex.by/maps/?ll=27.542023%2C53.902241&z=17&pt=27.542023%2C53.902241'
    },
    google: {
      title: 'Google Maps: Minsk, Korolya Street, 9',
      embed: 'https://maps.google.com/maps?q=53.902241%2C27.542023&z=16&output=embed',
      link: 'https://maps.google.com/?q=53.902241,27.542023'
    }
  };
  mapChoices.forEach(choice => choice.addEventListener('click', () => {
    const map = maps[choice.dataset.mapChoice];
    if (!map) return;
    mapFrame.src = map.embed;
    mapFrame.title = map.title;
    mapOpen.href = map.link;
    mapChoices.forEach(button => {
      const selected = button === choice;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  }));

  form.elements.name.addEventListener('input', () => form.elements.name.setCustomValidity(''));

  phone.addEventListener('input', () => {
    phone.setCustomValidity('');
    phone.classList.remove('invalid');
  });
  phone.addEventListener('blur', () => {
    if (!phone.value.trim()) return;
    const normalized = normalizePhone(phone.value);
    if (normalized) phone.value = normalized.replace(/(\+375)(\d{2})(\d{3})(\d{2})(\d{2})/, '$1 ($2) $3-$4-$5');
  });

  document.querySelectorAll('.add-coffee').forEach(button => {
    button.addEventListener('click', () => {
      setInterest('order');
      form.elements.comment.value = `Интересует: ${button.dataset.item}.`;
      document.querySelector('#request').scrollIntoView({ behavior: 'smooth' });
      window.setTimeout(() => form.elements.name.focus({ preventScroll: true }), 550);
    });
  });
  document.querySelectorAll('[data-interest="subscription"]').forEach(link => {
    link.addEventListener('click', () => { setInterest('subscription'); });
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    message.textContent = '';
    message.className = 'form-message';
    if (form.elements.website.value) return;
    if (!form.elements.name.value.trim()) {
      form.elements.name.setCustomValidity('Напишите, как к вам обращаться.');
      form.elements.name.reportValidity();
      form.elements.name.focus();
      return;
    }
    const normalized = normalizePhone(phone.value);
    if (!normalized) {
      phone.setCustomValidity('Введите белорусский номер, например +375 29 123-45-67.');
      phone.classList.add('invalid');
      phone.reportValidity();
      phone.focus();
      return;
    }
    phone.setCustomValidity('');
    const previous = JSON.parse(sessionStorage.getItem('kvarta:last-lead') || 'null');
    if (previous && previous.phone === normalized && Date.now() - previous.at < 10 * 60 * 1000) {
      message.textContent = 'Заявка уже у нас. Скоро позвоним!';
      message.classList.add('success');
      return;
    }
    submit.disabled = true;
    label.textContent = 'Отправляем…';
    const payload = {
      name: form.elements.name.value.trim(),
      phone: normalized,
      interest: form.elements.interest.value,
      comment: form.elements.comment.value.trim(),
      website: form.elements.website.value,
      attribution
    };
    try {
      const response = await fetch('/api/lead', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const result = await response.json();
      if (!response.ok || !result.ok) {
        const error = new Error(result.error || 'Не получилось отправить заявку.');
        error.status = response.status;
        throw error;
      }
      sessionStorage.setItem('kvarta:last-lead', JSON.stringify({ phone: normalized, at: Date.now() }));
      message.textContent = result.message;
      message.classList.add('success');
      label.textContent = 'Заявка отправлена';
      form.reset();
      window.setTimeout(() => { label.textContent = 'Отправить заявку'; }, 4000);
    } catch (error) {
      message.textContent = error.status === 429
        ? error.message
        : `${error.message || 'Не удалось отправить заявку.'} Проверьте интернет и попробуйте ещё раз.`;
      message.classList.add('error');
      label.textContent = 'Попробовать ещё раз';
    } finally {
      submit.disabled = false;
    }
  });

  const menuButton = document.querySelector('.menu-toggle');
  const nav = document.querySelector('.main-nav');
  menuButton.addEventListener('click', () => {
    const opened = nav.classList.toggle('open');
    menuButton.setAttribute('aria-expanded', String(opened));
  });
  nav.querySelectorAll('a').forEach(link => link.addEventListener('click', () => {
    nav.classList.remove('open');
    menuButton.setAttribute('aria-expanded', 'false');
  }));

  if ('IntersectionObserver' in window && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('in-view');
        observer.unobserve(entry.target);
      }
    }), { threshold: 0.12 });
    document.querySelectorAll('.coffee-row, .time-stop, .visit-card, .sub-copy, .quote-section blockquote').forEach(element => {
      element.classList.add('reveal');
      observer.observe(element);
    });
  }
})();
