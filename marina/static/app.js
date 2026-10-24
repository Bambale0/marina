"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...children) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const c of children.flat())
    if (c != null)
      n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
};
let token = sessionStorage.getItem("marina-token"),
  me,
  current,
  selected = sessionStorage.getItem("subject");
const states = {
  awaiting_video: "Ожидаем видео",
  video_received: "Видео получено",
  in_review: "На просмотре",
  conclusion_sent: "Заключение отправлено",
  invited: "Приглашён на диагностику",
  booked: "Записан",
  awaiting_payment: "Ожидает оплаты",
  prepared: "Готов к запуску",
  refunded: "Возвращено",
  paid_review: "Оплата требует сверки",
  draft: "Черновик",
  approved: "Утверждено",
  published: "Опубликовано",
  archived: "В архиве",
  requested: "Новая заявка",
  proposed: "Предложено время",
  confirmed: "Подтверждено",
  reschedule_requested: "Нужно другое время",
  pending: "Ожидает оплаты",
  paid: "Оплачено",
  submitted: "У специалиста",
  in_progress: "В работе",
  answered: "Ответ готов",
  followup_pending: "Принято уточнение",
  closed: "Завершено",
  completed: "Завершено",
  started: "Начато",
  active: "Действует",
  open: "Требует решения",
  ready: "Готово",
  queued: "В очереди",
  uncertain: "Нужна сверка",
  revoked: "Отозвано",
  valid: "Расчёт готов",
};
const kinds = {
  video_review: "Видео и диагностика",
  plan: "Будущий комплекс",
  media: "Медиафайл",
  retention_task: "Срок хранения файла",
  data_request: "Запрос по данным",
  payment_event: "Платёжное событие",
  refund: "Возврат",
  ai_job: "ИИ-задание",
  decision: "Решения запуска",
  material: "Материалы",
  method: "Методики",
  product: "Продукты и тарифы",
  policy: "Бизнес-правила",
  exercise: "Упражнения",
  selection: "Правила подбора",
  course: "Курсы",
  template: "Шаблоны",
  onboarding: "Экраны знакомства",
  funnel: "Сценарии скрининга",
  funnel_run: "Прохождение скрининга",
  funnel_entry: "Вход в скрининг",
  bot_profile: "Профиль Telegram",
  report: "Отчёт PDF",
  case: "Разбор",
  booking: "Встреча",
  right: "Право доступа",
  order: "Заказ",
  lesson: "Занятие",
  attempt: "Скрининг",
  payment_attempt: "Попытка оплаты",
  reconciliation: "Сверка",
  progress: "Прогресс",
  quote: "Расчёт цены",
};
function notice(text, error = false) {
  const n = $("#notice");
  n.textContent = text;
  n.className = error ? "error" : "";
  n.style.display = "block";
  clearTimeout(notice.timer);
  notice.timer = setTimeout(() => (n.style.display = "none"), 6000);
}
async function api(path, method = "GET", data) {
  const headers = { Authorization: "Bearer " + token };
  if (data !== undefined && !(data instanceof FormData))
    headers["Content-Type"] = "application/json";
  const r = await fetch("/api" + path, {
    method,
    headers,
    body:
      data === undefined
        ? undefined
        : data instanceof FormData
          ? data
          : JSON.stringify(data),
  });
  if (!r.ok) {
    if (r.status === 401 && !path.startsWith("/auth/")) {
      token = null;
      sessionStorage.removeItem("marina-token");
      setTimeout(() => location.reload(), 0);
    }
    const err = await r.json();
    throw new Error(err.error || "Ошибка запроса");
  }
  return r.json();
}
async function safe(fn) {
  try {
    await fn();
  } catch (e) {
    notice(e.message, true);
  }
}
const button = (text, fn, secondary = false) =>
  el(
    "button",
    {
      type: "button",
      class: secondary ? "secondary" : "",
      onclick: () => safe(fn),
    },
    text,
  );
const badge = (s) => el("span", { class: "badge" }, states[s] || s);
const dateTime = (t) =>
  t
    ? new Intl.DateTimeFormat("ru", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "Europe/Moscow",
      }).format(new Date(t * 1000)) + " МСК"
    : "—";
const main = (...nodes) => $("#main").replaceChildren(...nodes);
const heading = (title, sub) =>
  el(
    "div",
    {},
    el("h1", {}, title),
    sub ? el("p", { class: "muted" }, sub) : null,
  );
const empty = (text) => el("div", { class: "empty" }, text);
const key = () => crypto.randomUUID();
function field(label, type = "text", value = "") {
  const input = el(
    type === "textarea" ? "textarea" : "input",
    type === "textarea" ? {} : { type },
  );
  if (type === "checkbox") input.checked = Boolean(value);
  else input.value = value ?? "";
  return {
    node: el("label", {}, label, input),
    input,
    get: () =>
      type === "checkbox"
        ? input.checked
        : type === "number"
          ? Number(input.value)
          : input.value,
  };
}
function selectField(label, values, value) {
  const input = el("select", { "aria-label": label });
  for (const [v, t] of values) input.append(el("option", { value: v }, t));
  if (value != null) input.value = value;
  return { node: el("label", {}, label, input), input, get: () => input.value };
}
function form(title, fields, submitText, submit) {
  const f = el("form", {}, el("h2", {}, title), ...fields.map((f) => f.node));
  const b = el("button", { type: "submit" }, submitText);
  f.append(b);
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    safe(async () => {
      b.disabled = true;
      try {
        await submit();
      } finally {
        b.disabled = false;
      }
    });
  });
  return f;
}
function modal(node) {
  $("#dialog-body").replaceChildren(node);
  $("#dialog").showModal();
}
$("#close-dialog").onclick = () => $("#dialog").close();
const subjectPicker = () =>
  selectField(
    "Для кого",
    me.subjects
      .filter((s) => s.status === "active")
      .map((s) => [
        s.id,
        s.data.name + (s.data.kind === "adult" ? " · взрослый" : " · ребёнок"),
      ]),
    selected,
  );
async function refresh() {
  me = await api("/me");
  if (!selected || !me.subjects.some((s) => s.id === selected))
    selected = me.subjects[0]?.id;
  $("#identity").textContent =
    me.account.name + (me.account.role === "admin" ? " · специалист" : "");
  const nav = $("#navigation");
  nav.replaceChildren();
  const pages = [
    ["home", "Обзор", home],
    ["profiles", "Профили", profiles],
    ["screenings", "Скрининги", screenings],
    ["training", "Занятия", training],
    ["courses", "Курсы", courses],
    ["history", "Мои обращения", history],
    ["data", "Мои данные", dataPage],
  ];
  if (me.account.role === "admin")
    pages.push(
      ["queue", "Очередь специалиста", adminQueue],
      ["crm", "CRM", adminCrm],
      ["content", "Контент и настройки", adminContent],
      ["accounts", "Клиенты", adminAccounts],
      ["operations", "Эксплуатация", operations],
      ["objects", "Все объекты", adminObjects],
      ["analytics", "Отчёты", analyticsPage],
      ["audit", "Аудит", auditPage],
    );
  for (const [id, title, fn] of pages) {
    const b = button(
      title,
      async () => {
        current = id;
        nav
          .querySelectorAll("button")
          .forEach((n) => n.classList.remove("active"));
        b.classList.add("active");
        await fn();
      },
      true,
    );
    nav.append(b);
  }
  nav.append(
    button(
      "Выйти",
      async () => {
        await api("/logout", "POST", {});
        sessionStorage.removeItem("marina-token");
        location.reload();
      },
      true,
    ),
  );
}
async function home() {
  const s = me.subjects.find((s) => s.id === selected);
  main(
    el(
      "section",
      { class: "hero" },
      el("p", { class: "eyebrow" }, "ВАШЕ ПРОСТРАНСТВО ПОДДЕРЖКИ"),
      el("h1", {}, "В своём темпе.\nКаждый день."),
      el(
        "p",
        {},
        "Выберите следующий шаг: понять особенности развития, обратиться к специалисту или начать занятия.",
      ),
      button("Выбрать профиль", profiles),
    ),
    el(
      "div",
      { class: "grid" },
      el(
        "section",
        { class: "card" },
        el("span", { class: "badge" }, "Бесплатный первый шаг"),
        el("h2", {}, "Понять, что важно"),
        el(
          "p",
          {},
          "Короткие анкеты по утверждённым методикам. Результат скрининга не является диагнозом.",
        ),
        button("Перейти к скринингам", screenings),
      ),
      el(
        "section",
        { class: "card" },
        el("span", { class: "badge" }, "Помощь специалиста"),
        el("h2", {}, "Обсудить вопрос"),
        el(
          "p",
          {},
          "Оставьте заявку на встречу. Специалист предложит время, а вы подтвердите удобный вариант.",
        ),
        button("Записаться", newBooking),
      ),
      el(
        "section",
        { class: "card" },
        el("span", { class: "badge" }, "10–15 минут в день"),
        el("h2", {}, "Заниматься дома"),
        el(
          "p",
          {},
          me.trial
            ? "Пробный доступ до " + dateTime(me.trial.ends)
            : "Первые 72 часа доступны бесплатно с первого запуска готового комплекса.",
        ),
        button("Открыть занятия", training),
      ),
    ),
  );
}
async function profiles() {
  if (!(await privacyGate())) return;
  await refresh();
  const cards = me.subjects.map((s) =>
    el(
      "article",
      { class: "card" },
      badge(s.status),
      el("h2", {}, s.data.name),
      el(
        "p",
        {},
        (s.data.kind === "adult" ? "Взрослый" : "Ребёнок") +
          " · " +
          s.data.birth_date,
      ),
      button(
        s.id === selected ? "Выбран" : "Выбрать",
        async () => {
          selected = s.id;
          sessionStorage.setItem("subject", selected);
          await profiles();
        },
        true,
      ),
      button(
        "Изменить",
        async () => {
          const name = field("Имя", "text", s.data.name),
            born = field("Дата рождения", "date", s.data.birth_date);
          modal(
            form("Изменить профиль", [name, born], "Сохранить", async () => {
              await api("/subjects/" + s.id, "PATCH", {
                revision: s.revision,
                name: name.get(),
                birth_date: born.get(),
              });
              $("#dialog").close();
              await profiles();
            }),
          );
        },
        true,
      ),
    ),
  );
  const name = field("Имя"),
    born = field("Дата рождения", "date"),
    kind = selectField("Тип профиля", [
      ["adult", "Для себя"],
      ["child", "Для ребёнка"],
    ]);
  main(
    heading(
      "Ваши профили",
      "Результаты, занятия и покупки закреплены за выбранным человеком.",
    ),
    el("div", { class: "grid" }, ...cards),
    el(
      "section",
      { class: "card spacer" },
      form("Добавить профиль", [kind, name, born], "Создать", async () => {
        const s = await api("/subjects", "POST", {
          kind: kind.get(),
          name: name.get(),
          birth_date: born.get(),
        });
        selected = s.id;
        await profiles();
      }),
    ),
  );
}
async function screenings() {
  const pick = subjectPicker();
  const data = await api(
    "/catalog/method" + (pick.get() ? "?subject_id=" + pick.get() : ""),
  );
  pick.input.onchange = () =>
    safe(async () => {
      selected = pick.get();
      await screenings();
    });
  main(
    heading(
      "Скрининги",
      "Только опубликованные специалистом методики. Скрининг не ставит диагноз.",
    ),
    pick.node,
    el(
      "div",
      { class: "grid" },
      ...(data.items.length
        ? data.items.map((m) =>
            el(
              "article",
              { class: "card" },
              el("h2", {}, m.data.title),
              el("p", {}, m.data.limitations),
              el("small", {}, m.data.source),
              el(
                "div",
                { class: "spacer" },
                button("Пройти анкету", async () => {
                  const a = await api("/attempts", "POST", {
                    method_id: m.id,
                    subject_id: pick.get(),
                    key: key(),
                  });
                  await attempt(a.id);
                }),
              ),
            ),
          )
        : [
            empty(
              "Специалист пока готовит методики. Доступные анкеты появятся здесь после утверждения.",
            ),
          ]),
    ),
  );
}
async function attempt(id) {
  const a = await api("/attempts/" + id);
  if (a.status === "completed") {
    main(
      heading("Результат скрининга"),
      el(
        "article",
        { class: "card" },
        el("h2", {}, a.data.result.text),
        el("p", {}, a.data.result.limitations),
      ),
      el(
        "div",
        { class: "row spacer" },
        button("Подробный PDF", () =>
          shop("pdf", a.subject, { attempt_id: a.id }),
        ),
        button("Разбор специалиста", () => shop("case", a.subject)),
        button("Записаться на встречу", () => newBooking(a.subject), true),
        button("Перейти к занятиям", training, true),
      ),
    );
    return;
  }
  const fields = [];
  const f = el(
    "form",
    {},
    heading(
      a.data.method_snapshot.title,
      "Ответы сохраняются, можно продолжить позже.",
    ),
  );
  for (const q of a.data.method_snapshot.questions) {
    if (q.when && a.data.answers[q.when.question] !== q.when.answer) continue;
    const input = selectField(
      q.text,
      [["", "Выберите ответ"], ...q.options.map((o) => [o.id, o.text])],
      a.data.answers[q.id] || "",
    );
    fields.push([q, input]);
    f.append(input.node);
  }
  const save = async () => {
    const answers = {};
    for (const [q, input] of fields)
      if (input.get()) answers[q.id] = input.get();
    for (const q of a.data.method_snapshot.questions)
      if (q.when && answers[q.when.question] !== q.when.answer)
        delete answers[q.id];
    return api("/attempts/" + id, "PATCH", { revision: a.revision, answers });
  };
  f.append(
    el(
      "div",
      { class: "row" },
      button("Сохранить и продолжить", async () => {
        await save();
        notice("Ответы сохранены");
        await attempt(id);
      }),
      button(
        "Получить результат",
        async () => {
          const saved = await save();
          await api("/attempts/" + id + "/complete", "POST", {
            revision: saved.revision,
          });
          await attempt(id);
        },
        true,
      ),
    ),
  );
  main(f);
}
async function newBooking(subject) {
  if (!me.subjects.length) return profiles();
  const pick = subjectPicker();
  if (subject) pick.input.value = subject;
  const topic = field("Что хотите обсудить?", "textarea"),
    prefs = field("Удобные дни и время", "textarea");
  main(
    form(
      "Запись на встречу",
      [pick, topic, prefs],
      "Отправить заявку",
      async () => {
        await api("/bookings", "POST", {
          subject_id: pick.get(),
          topic: topic.get(),
          preferences: prefs.get(),
          key: key(),
        });
        notice("Заявка отправлена. Специалист предложит время.");
        await history();
      },
    ),
  );
}
async function shop(service, subject, extra = {}) {
  const products = (await api("/catalog/product")).items.filter(
    (p) =>
      p.data.service === service &&
      p.data.currency === "XTR" &&
      (!extra.course_id || p.data.course_id === extra.course_id),
  );
  const pick = subjectPicker();
  if (subject) pick.input.value = subject;
  main(
    heading(
      "Выберите услугу",
      "Цена и условия рассчитываются сервером до оплаты.",
    ),
    pick.node,
    el(
      "div",
      { class: "grid" },
      ...(products.length
        ? products.map((p) =>
            el(
              "article",
              { class: "card" },
              el("h2", {}, p.data.title),
              el("p", {}, p.data.terms),
              el("h3", {}, p.data.amount + " Stars"),
              button(
                service === "case" && !extra.case_id
                  ? "Подготовить комплект"
                  : "Рассчитать",
                async () => {
                  if (service === "case" && !extra.case_id) {
                    const draft = await api("/cases", "POST", {
                      product_id: p.id,
                      subject_id: pick.get(),
                      key: key(),
                    });
                    return clientCard(draft);
                  }
                  const q = await api("/quotes", "POST", {
                    product_id: p.id,
                    subject_ids: [pick.get()],
                    surface: "telegram",
                    ...extra,
                  });
                  await quoteView(q);
                },
              ),
            ),
          )
        : [empty("Продажи этой услуги пока не открыты.")]),
    ),
  );
}
async function quoteView(q) {
  main(
    heading("Проверьте условия покупки"),
    el(
      "section",
      { class: "card" },
      el("h2", {}, q.data.product_snapshot.title),
      el("p", {}, q.data.product_snapshot.terms),
      ...q.data.lines.map((l) =>
        el(
          "p",
          {},
          (me.subjects.find((s) => s.id === l.subject_id)?.data.name ||
            l.subject_id) +
            " · " +
            l.amount +
            " " +
            q.data.currency +
            " · скидка " +
            l.discount +
            "%" +
            (l.ends
              ? " · " + dateTime(l.starts) + " — " + dateTime(l.ends)
              : ""),
        ),
      ),
      el("h2", {}, "Итого: " + q.data.total + " " + q.data.currency),
      el(
        "p",
        { class: "quiet" },
        "Предложение действует до " + dateTime(q.data.expires),
      ),
      button("Перейти к оплате", async () => {
        const order = await api("/orders", "POST", {
          quote_id: q.id,
          key: key(),
        });
        await api("/orders/" + order.id + "/checkout", "POST", {});
        notice("Счёт создаётся. Он появится в разделе «Мои обращения».");
        await history();
      }),
    ),
  );
}
async function training() {
  const pick = subjectPicker(),
    equipment = field("Доступные предметы (через запятую)"),
    constraints = field("Ограничения, указанные специалистом (через запятую)");
  main(
    heading(
      "Занятия дома",
      "Готовый комплекс на 10–15 минут. Trial начинается только при первом запуске.",
    ),
    form(
      "Подобрать занятие",
      [pick, equipment, constraints],
      "Начать",
      async () => {
        const lesson = await api("/lessons/start", "POST", {
          subject_id: pick.get(),
          equipment: equipment
            .get()
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
          constraints: constraints
            .get()
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
          key: key(),
        });
        await lessonView(lesson.id);
      },
    ),
    el(
      "div",
      { class: "spacer" },
      button(
        "Купить или продлить подписку",
        () => shop("subscription", pick.get()),
        true,
      ),
    ),
  );
}
async function authenticatedFile(path) {
  const r = await fetch("/api" + path, {
    headers: { Authorization: "Bearer " + token },
  });
  if (!r.ok) throw new Error("Файл недоступен");
  return URL.createObjectURL(await r.blob());
}
async function lessonView(id) {
  const l = await api("/lessons/" + id);
  const checks = [];
  const content = el(
    "div",
    { class: "stack" },
    heading(
      "Ваше занятие",
      Math.round(l.data.duration_seconds / 60) + " минут",
    ),
  );
  for (const ex of l.data.exercises) {
    const done = field(
      "Выполнено",
      "checkbox",
      l.data.completed_exercises.includes(ex.id),
    );
    checks.push([ex, done]);
    const card = el(
      "article",
      { class: "card" },
      el("h2", {}, ex.title),
      el("p", { class: "readable" }, ex.instructions),
      button(
        "Показать видео",
        async () => {
          const src = await authenticatedFile(
            "/lessons/" + id + "/video/" + ex.id,
          );
          const v = el("video", { controls: "", src });
          v.onended = () => URL.revokeObjectURL(src);
          card.append(v);
        },
        true,
      ),
      done.node,
    );
    content.append(card);
  }
  const feedback = selectField("Как прошло занятие?", [
    ["suitable", "Подходящая сложность"],
    ["easy", "Легко"],
    ["hard", "Сложно"],
    ["stop", "Остановились, нужен специалист"],
  ]);
  content.append(
    button(
      "Сохранить прогресс",
      async () => {
        await api("/lessons/" + id + "/progress", "PATCH", {
          revision: l.revision,
          completed_exercises: checks
            .filter(([, f]) => f.get())
            .map(([e]) => e.id),
        });
        notice("Прогресс сохранён");
        await lessonView(id);
      },
      true,
    ),
    feedback.node,
    button("Завершить занятие", async () => {
      await api("/lessons/" + id + "/complete", "POST", {
        completed_exercises: checks
          .filter(([, f]) => f.get())
          .map(([e]) => e.id),
        feedback: feedback.get(),
      });
      notice("Занятие сохранено");
      await history();
    }),
  );
  main(content);
}
async function courses() {
  const pick = subjectPicker();
  if (!pick.get()) return profiles();
  const published = (await api("/catalog/course?subject_id=" + pick.get()))
    .items;
  const owned = (await api("/my-courses")).items.filter(
    (item) => item.right.subject === pick.get(),
  );
  const all = new Map(published.map((course) => [course.id, course]));
  owned.forEach((item) => all.set(item.course.id, item.course));
  pick.input.onchange = () =>
    safe(async () => {
      selected = pick.get();
      await courses();
    });
  const cards = [...all.values()].map((course) => {
    const access = owned.find(
      (item) => item.course.id === course.id && item.accessible,
    );
    return el(
      "article",
      { class: "card" },
      el("h2", {}, course.data.title),
      el("p", {}, course.data.instructions),
      ...(access
        ? course.data.lessons.map((lesson) =>
            button(
              lesson.title,
              () => courseLesson(course.id, lesson.id, pick.get()),
              true,
            ),
          )
        : course.status === "published"
          ? [
              button("Выбрать тариф", () =>
                shop("course", pick.get(), { course_id: course.id }),
              ),
            ]
          : [el("p", { class: "muted" }, "Доступ к этой редакции завершён")]),
    );
  });
  main(
    heading(
      "Тематические курсы",
      "Отдельная покупка и прогресс выбранного профиля.",
    ),
    pick.node,
    el(
      "div",
      { class: "grid" },
      ...(cards.length ? cards : [empty("Программы курсов пока готовятся.")]),
    ),
  );
}
async function courseLesson(course, id, subject) {
  const l = await api(
    "/courses/" + course + "/lessons/" + id + "?subject_id=" + subject,
  );
  main(
    heading(l.title),
    el("section", { class: "card readable" }, l.text),
    el(
      "div",
      { class: "spacer" },
      button(
        l.completed ? "Урок завершён" : "Отметить завершение",
        async () => {
          await api(
            "/courses/" + course + "/lessons/" + id + "/complete",
            "POST",
            { subject_id: subject },
          );
          await courses();
        },
      ),
    ),
  );
  for (const ex of l.exercises || []) {
    const card = el(
      "section",
      { class: "card spacer" },
      el("h2", {}, ex.title),
      el("p", { class: "readable" }, ex.instructions),
    );
    card.append(
      button(
        "Показать видео урока",
        async () => {
          const src = await authenticatedFile(
            "/courses/" +
              course +
              "/lessons/" +
              id +
              "/videos/" +
              ex.id +
              "?subject_id=" +
              subject,
          );
          card.append(el("video", { src, controls: "" }));
        },
        true,
      ),
    );
    $("#main").append(card);
  }
}
async function history() {
  const items = (await api("/history")).items.filter(
    (o) => !["quote", "right", "progress", "payment_attempt"].includes(o.kind),
  );
  main(
    heading(
      "Мои обращения",
      "Здесь сохраняются результаты, заказы и история занятий.",
    ),
    el(
      "div",
      { class: "stack" },
      ...(items.length
        ? items.map((o) =>
            el(
              "article",
              { class: "card" },
              el(
                "div",
                { class: "row between" },
                el("h3", {}, kinds[o.kind] || o.kind),
                badge(o.status),
              ),
              el("p", { class: "quiet" }, dateTime(o.created)),
              button("Открыть", () => clientCard(o), true),
            ),
          )
        : [empty("Выберите профиль и свой первый шаг.")]),
    ),
  );
}
async function clientCard(o) {
  if (o.kind === "attempt") return attempt(o.id);
  if (o.kind === "lesson") return lessonView(o.id);
  const d = o.data,
    card = el(
      "section",
      { class: "card stack" },
      heading(kinds[o.kind] || o.kind),
      badge(o.status),
    );
  if (o.kind === "report") {
    card.append(el("p", {}, "Срок готовности: " + dateTime(d.due_at)));
    if (d.pdf_id)
      card.append(
        button("Скачать PDF", async () => {
          const url = await authenticatedFile("/reports/" + o.id + "/pdf");
          const a = el("a", { href: url, download: "report.pdf" });
          a.click();
          setTimeout(() => URL.revokeObjectURL(url), 5000);
        }),
      );
  }
  if (o.kind === "case") {
    if (d.paid_at == null)
      card.append(
        el(
          "p",
          {},
          "Срок специалиста начнётся после полного комплекта и оплаты.",
        ),
        button("Оплатить этот разбор", () =>
          shop("case", o.subject, { case_id: o.id }),
        ),
      );
    if (!d.submitted_at) {
      const problem = field(
          "Одна проблема, которую хотите разобрать",
          "textarea",
        ),
        questionFields = (
          d.product_snapshot.case_questions || [
            { id: "context", text: "Опишите ситуацию" },
          ]
        ).map((q) => ({ id: q.id, ...field(q.text, "textarea") }));
      const upload = field("Видео или аудио", "file");
      card.append(
        upload.node,
        button(
          "Загрузить материал",
          async () => {
            const f = new FormData();
            f.append("file", upload.input.files[0]);
            await api("/cases/" + o.id + "/media", "POST", f);
            notice("Материал сохранён");
          },
          true,
        ),
        form(
          "Отправить полный комплект",
          [problem, ...questionFields],
          "Отправить специалисту",
          async () => {
            await api("/cases/" + o.id + "/submit", "POST", {
              problem: problem.get(),
              answers: Object.fromEntries(
                questionFields.map((f) => [f.id, f.get()]),
              ),
            });
            await history();
          },
        ),
      );
    }
    if (d.answer)
      card.append(
        el("p", { class: "readable" }, d.answer),
        el("p", {}, "Уточнение доступно до " + dateTime(d.followup_until)),
      );
    for (const id of d.answer_attachments || [])
      card.append(
        button(
          "Открыть материал ответа",
          async () => {
            const src = await authenticatedFile("/media/" + id);
            window.open(src, "_blank", "noopener");
          },
          true,
        ),
      );
    if (o.status === "answered") {
      const q = field("Один вопрос по той же проблеме", "textarea");
      card.append(
        form("Уточнение", [q], "Отправить", async () => {
          await api("/cases/" + o.id + "/followup", "POST", {
            text: q.get(),
            same_problem: true,
          });
          await history();
        }),
      );
    }
    if (d.followup)
      card.append(
        el("p", {}, d.followup.text),
        el(
          "p",
          {},
          d.followup.answer ||
            "Специалист ответит до " + dateTime(d.followup.due_at),
        ),
      );
  }
  if (o.kind === "booking") {
    card.append(el("p", {}, d.topic));
    const p = d.proposals?.find((p) => p.id === d.current_proposal);
    if (p)
      card.append(
        el("p", {}, dateTime(p.starts) + " · " + p.duration_minutes + " минут"),
      );
    if (o.status === "proposed")
      card.append(
        button("Подтвердить время", async () => {
          await api("/bookings/" + o.id + "/confirm", "POST", {
            proposal_id: p.id,
          });
          await history();
        }),
        button(
          "Предложить другое время",
          async () => {
            await api("/bookings/" + o.id + "/reschedule", "POST", {});
            await history();
          },
          true,
        ),
      );
    if (d.connection)
      card.append(
        el(
          "a",
          { href: d.connection, target: "_blank", rel: "noopener" },
          "Подключиться к встрече ↗",
        ),
      );
  }
  if (o.kind === "order") {
    card.append(
      el("h2", {}, d.total + " " + d.currency),
      el("p", {}, d.product_snapshot.title),
    );
    if (o.status === "pending") {
      const attempts = (await api("/history")).items.filter(
        (a) => a.kind === "payment_attempt" && a.data.order_id === o.id,
      );
      const a = attempts[0];
      if (a?.data.url)
        card.append(
          button("Оплатить", () => {
            if (window.Telegram?.WebApp?.initData && d.provider === "stars")
              Telegram.WebApp.openInvoice(a.data.url, () => safe(history));
            else window.open(a.data.url, "_blank", "noopener");
          }),
        );
      else
        card.append(
          button(
            "Проверить счёт",
            async () => {
              await api("/orders/" + o.id + "/checkout", "POST", {});
              notice(
                "Счёт обрабатывается. Обновите карточку через несколько секунд.",
              );
              await history();
            },
            true,
          ),
        );
    }
  }
  main(card);
}
safe(async () => {
  const webapp = window.Telegram?.WebApp;
  if (webapp?.initData && !token) {
    const auth = await api("/auth/telegram", "POST", {
      init_data: webapp.initData,
    });
    token = auth.token;
    sessionStorage.setItem("marina-token", token);
    webapp.ready();
    webapp.expand();
  }
  if (token) {
    await refresh();
    await home();
  }
});
if (!token) {
  const code = field("Одноразовый код из команды /login в боте");
  $("#main").append(
    el(
      "section",
      { class: "card" },
      form("Вход по коду", [code], "Войти", async () => {
        const auth = await api("/auth/code", "POST", { code: code.get() });
        token = auth.token;
        sessionStorage.setItem("marina-token", token);
        await refresh();
        await home();
      }),
    ),
  );
}

async function privacyGate() {
  const data = await api("/privacy");
  if (data.accepted) return true;
  if (!data.policy) {
    main(
      heading("Скоро здесь можно будет начать"),
      empty(
        "Специалист готовит условия обработки данных. Ввод данных откроется после публикации.",
      ),
    );
    return false;
  }
  main(
    heading("Условия обработки данных"),
    el("section", { class: "card readable" }, data.policy.data.terms),
    button("Прочитал(а) и принимаю эту редакцию", async () => {
      await api("/consents", "POST", {
        policy_id: data.policy.id,
        accepted: true,
      });
      await profiles();
    }),
  );
  return false;
}

async function dataPage() {
  const privacy = await api("/privacy");
  main(
    heading("Мои данные"),
    privacy.policy
      ? el("section", { class: "card readable" }, privacy.policy.data.terms)
      : empty("Условия обработки данных ещё готовятся."),
    el(
      "div",
      { class: "row spacer" },
      button("Выгрузить мои данные", async () => {
        const data = await api("/data-export");
        const url = URL.createObjectURL(
          new Blob([JSON.stringify(data, null, 2)], {
            type: "application/json",
          }),
        );
        const a = el("a", { href: url, download: "my-data.json" });
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      }),
      button(
        "Запросить удаление данных",
        async () => {
          await api("/data-requests", "POST", { type: "delete" });
          notice(
            "Запрос принят специалистом. Удаление выполняется по утверждённой политике и не происходит автоматически.",
          );
        },
        true,
      ),
    ),
  );
}
