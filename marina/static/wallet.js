"use strict";

// An independent browser page, not a Telegram Mini App or Telegram checkout.
const root = document.getElementById("wallet-view");
const banner = document.getElementById("notice");
let token = sessionStorage.getItem("marina-web-wallet-token") || "";
let wallet = null;
let currentOrder = sessionStorage.getItem("marina-web-wallet-order") || "";

function make(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (name === "class") node.className = value;
    else if (name === "text") node.textContent = value;
    else if (name === "type" || name === "placeholder" || name === "href" || name === "target" || name === "rel" || name === "min" || name === "value") node.setAttribute(name, value);
    else if (name === "required") node.required = Boolean(value);
  }
  for (const child of children.flat()) if (child != null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}

function button(text, onClick) {
  const b = make("button", { type: "button" }, text);
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await onClick(); }
    catch (error) { showError(error.message || "Не удалось выполнить запрос"); }
    finally { b.disabled = false; }
  });
  return b;
}

function showError(message) {
  banner.textContent = message;
  banner.className = "error";
  banner.style.display = "block";
}

async function api(path, method = "GET", data = undefined) {
  const headers = {};
  if (token) headers.Authorization = "Bearer " + token;
  if (data !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch("/api" + path, {
    method,
    headers,
    body: data === undefined ? undefined : JSON.stringify(data),
    cache: "no-store",
    credentials: "same-origin",
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/auth/code") {
      token = "";
      sessionStorage.removeItem("marina-web-wallet-token");
    }
    throw new Error(body.error || "Ошибка запроса (" + response.status + ")");
  }
  return body;
}

function loginForm() {
  const form = make("form", { class: "card stack" },
    make("h2", {}, "Вход в личный кабинет"),
    make("p", {}, "Получите одноразовый код командой /login в личном чате с ботом. Код действует 5 минут."),
  );
  const code = make("input", { type: "text", placeholder: "Одноразовый код", required: true });
  const submit = make("button", { type: "submit" }, "Войти");
  form.append(make("label", {}, "Код из Telegram", code), submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const result = await api("/auth/code", "POST", { code: code.value.trim() });
      token = result.token;
      code.value = "";
      sessionStorage.setItem("marina-web-wallet-token", token);
      await loadWallet();
    } catch (error) { showError(error.message); }
    finally { submit.disabled = false; }
  });
  root.replaceChildren(form);
}

async function loadWallet() {
  if (!token) { loginForm(); return; }
  try { wallet = await api("/wallet"); }
  catch (error) {
    if (!token) { loginForm(); return; }
    showError(error.message);
    return;
  }
  const page = make("section", { class: "card stack" },
    make("h2", {}, "Мой баланс: " + wallet.balance + " НК"),
    make("p", {}, "1 НК = 1 ₽ · вывод и переводы недоступны"),
  );
  page.append(button("Выйти", async () => {
    await api("/logout", "POST", {});
    token = "";
    currentOrder = "";
    sessionStorage.removeItem("marina-web-wallet-token");
    sessionStorage.removeItem("marina-web-wallet-order");
    loginForm();
  }));
  const history = make("section", { class: "card stack" }, make("h2", {}, "История операций"));
  const movements = wallet.history || [];
  if (movements.length) {
    for (const item of movements) {
      history.append(make("p", {}, (item.delta > 0 ? "+" : "") + item.delta
        + " НК · " + item.source
        + " · " + new Date(item.created * 1000).toLocaleString("ru")));
    }
  } else history.append(make("p", {}, "Операций пока нет."));
  const content = [page, history];
  if (!wallet.enabled || !wallet.web_topup_enabled) {
    content.push(make("section", { class: "card" },
      make("h2", {}, "Пополнение пока недоступно"),
      make("p", {}, "Специалист готовит утверждённые условия и безопасный платёжный маршрут Lava.top. Новые счета не создаются."),
    ));
  } else {
    const form = make("form", { class: "card stack" },
      make("h2", {}, "Пополнить через Lava.top"),
      make("p", {}, "После подтверждения платежа монеты будут зачислены на ваш баланс."),
    );
    const packageSelect = make("select");
    for (const pkg of wallet.packages) {
      packageSelect.append(make("option", { value: pkg.id }, pkg.title + " · " + pkg.coins + " ₽ / " + pkg.coins + " НК"));
    }
    const bookingSelect = make("select");
    bookingSelect.append(make("option", { value: "" }, "Просто пополнить баланс"));
    const payForBooking = (wallet.services || []).find(x => x.id === "online_lesson");
    if (payForBooking) {
      const items = (await api("/history")).items || [];
      for (const item of items) {
        if (item.kind === "booking" && item.status === "confirmed"
          && item.data && item.data.booking_kind === "online_lesson" && item.data.paid_at == null) {
          bookingSelect.append(make("option", { value: item.id },
            "Автоматически оплатить онлайн-занятие (" + payForBooking.coins + " НК)"));
        }
      }
    }
    const email = make("input", { type: "email", required: true, placeholder: "email@example.com" });
    const accepted = make("input", { type: "checkbox", required: true });
    const submit = make("button", { type: "submit" }, "Получить счёт Lava.top");
    form.append(
      make("details", {},
        make("summary", {}, "Прочитать условия использования Нейро-коинов"),
        make("p", { class: "wallet-terms" }, wallet.terms || "")
      ),
      make("label", {}, "Пакет", packageSelect),
      make("label", {}, "Действие после пополнения", bookingSelect),
      make("label", {}, "Email для чека", email),
      make("label", {}, accepted, " Принимаю условия предоставления Нейро-коинов и оферту."),
      submit,
      make("p", { class: "quiet" }, "Оплата не подтверждается кнопкой «Оплатил». Зачисление происходит только после проверки Lava.top."),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!accepted.checked) return;
      submit.disabled = true;
      try {
        const order = await api("/wallet/topups", "POST", {
          package_id: packageSelect.value,
          booking_id: bookingSelect.value || null,
          surface: "external_web",
          policy_id: wallet.policy_id,
          terms_accepted: true,
          key: crypto.randomUUID(),
        });
        currentOrder = order.id;
        sessionStorage.setItem("marina-web-wallet-order", currentOrder);
        await api("/orders/" + currentOrder + "/checkout", "POST", { email: email.value.trim() });
        await showPaymentStatus();
      } catch (error) { showError(error.message); }
      finally { submit.disabled = false; }
    });
    content.push(form);
  }
  if (currentOrder) content.push(make("section", { class: "card stack" },
    make("h2", {}, "Последний счёт"),
    button("Проверить статус счёта", showPaymentStatus),
  ));
  root.replaceChildren(...content);
}

async function showPaymentStatus() {
  if (!currentOrder) return;
  const result = await api("/wallet/topups/" + encodeURIComponent(currentOrder));
  const status = make("section", { class: "card stack" }, make("h2", {}, "Статус счёта"));
  if (result.status === "paid") {
    status.append(make("p", {}, "Платёж подтверждён. Текущий баланс: " + result.balance + " НК."));
  } else if (result.attempt_status === "uncertain") {
    status.append(make("p", {}, "Создание счёта требует проверки специалистом. Не создавайте повторный платёж."));
  } else if (result.payment_url) {
    const link = make("a", { href: result.payment_url, target: "_blank", rel: "noopener noreferrer" }, "Перейти к оплате через Lava.top");
    status.append(link, make("p", {}, "После оплаты вернитесь сюда и проверьте статус."));
  } else {
    status.append(make("p", {}, "Счёт обрабатывается. Повторно нажмите «Проверить статус» позднее."));
  }
  status.append(button("Обновить баланс", loadWallet));
  root.prepend(status);
}

loadWallet()
  .then(async () => {
    // A redirect status is never a payment confirmation; only the verified
    // server receipt can update the balance.
    if (token && currentOrder && new URLSearchParams(location.search).has("payment")) {
      await showPaymentStatus();
    }
  })
  .catch((error) => showError(error.message));
