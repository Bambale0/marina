"use strict";
const labels = {
  method: "Методика",
  welcome_text: "Приветствие",
  age_reminder_text: "Напоминание о выборе возраста",
  offer_text: "Предложение руководства",
  guide_title: "Название руководства",
  guide_pages: "Страницы руководства",
  guide_filename: "Имя PDF-файла",
  guide_product_id: "ID опубликованного тарифа руководства",
  free_material_id: "ID бесплатного PDF для зелёной зоны",
  refusal_material_id: "ID бесплатного PDF или видео после отказа",
  material_id: "ID опубликованного PDF-материала",
  asset_id: "ID файла материала",
  channel_url: "Ссылка на Telegram-канал",
  contact_url: "Личная ссылка Telegram специалиста",
  consultation_text: "Описание диагностики",
  consultation_intro_text: "Описание диагностики до покупки / после истечения промокода",
  consultation_valid_days: "Срок действия промокода, дней",
  consultation_code: "Промокод",
  consultation_amount_rub: "Цена диагностики по промокоду, ₽",
  consultation_regular_rub: "Обычная цена диагностики, ₽",
  success_text: "Сообщение после оплаты",
  cart_first_text: "Напоминание через 30 минут",
  cart_last_text: "Напоминание перед окончанием предложения",
  refusal_text: "Сообщение после отказа",
  followups: "Касания после выдачи руководства",
  hours: "Через сколько часов",
  price_rub: "Исходная цена из сценария, ₽ (справочно)",
  buyer_tag: "Тег покупателя",
  refused_tag: "Тег отказа",
  zone: "Зона результата (green / yellow / red)",
  age_group: "Возрастная группа (3-4 / 5-6 / 7-8 / 9-10)",
  concerns: "Вопросы экрана знакомства",
  description: "Пояснение после выбора вопроса",
  activities: "Варианты задания",
  observation_focus: "Что заметить",
  activity_note: "Примечание к заданию",
  activity_source: "Источник и контекст задания",
  selected_activity: "Выбранное задание",
  observation: "Наблюдение родителя",
  title: "Название",
  owner: "Ответственный",
  status: "Готовность",
  epics: "Эпики",
  evidence: "Основание решения",
  source: "Источник",
  rights: "Права использования",
  instructions: "Инструкция",
  audience: "Аудитория (adult / child)",
  min_months: "Минимальный возраст, месяцев",
  max_months: "Максимальный возраст, месяцев",
  limitations: "Ограничения вывода",
  questions: "Вопросы",
  id: "Идентификатор",
  text: "Текст",
  required: "Обязательный вопрос",
  options: "Варианты ответа",
  score: "Баллы",
  bands: "Диапазоны результата",
  min: "От",
  max: "До",
  examples: "Контрольные примеры",
  answers: "Ответы: вопрос=вариант, по строке",
  service: "Услуга (guide / pdf / case / subscription / course / booking)",
  amount: "Цена: копейки RUB или целые Stars",
  currency: "Валюта (RUB / XTR)",
  period_hours: "Период доступа, часов",
  terms: "Условия и основание",
  launch_approved: "Условия запуска проверены",
  web_sales_approved: "Веб-продажи согласованы",
  lava_offer_id: "Lava offer ID",
  required_media: "Обязательны медиафайлы",
  required_fields: "Обязательные поля анкеты",
  course_id: "Версия курса",
  duration_seconds: "Длительность, секунд",
  difficulty: "Сложность",
  adaptation: "Правило адаптации будущего занятия",
  allowed_difficulties: "Разрешённые уровни сложности (через запятую)",
  easy: "После отзыва «Легко»",
  suitable: "После отзыва «Подходит»",
  hard: "После отзыва «Сложно»",
  stop: "После остановки занятия",
  equipment: "Предметы, через запятую",
  exclusions: "Противопоказания, через запятую",
  video_id: "ID сохранённого видео",
  video_verified: "Специалист просмотрел видео",
  case_questions: "Вопросы анкеты разбора",
  refund_policy_approved: "Политика возвратов утверждена владельцем",
  alternatives: "ID утверждённых альтернатив, через запятую",
  when: "Ветвление (оставьте пустым, если вопрос показывается всегда)",
  question: "ID предыдущего вопроса",
  answer: "ID варианта, при котором показывается вопрос",
  type: "Тип правил",
  family_at: "Дата расчёта семьи (quote)",
  include_future: "Учитывать оплаченные будущие периоды",
  rank: "Порядок семьи (first_paid_then_created)",
  rounding: "Округление (half_up)",
  trial_purchase: "Покупка во время trial (immediately / after_trial)",
  quote_ttl_seconds: "Срок расчёта, секунд",
  renewal_window_hours: "Окно продления, часов",
  max_future_periods: "Лимит будущих периодов",
  late_payment: "Поздняя оплата (review)",
  pause_sales: "Приостановить продажи",
  max_open_orders: "Лимит незакрытых услуг",
  sla_reminder_hours: "За сколько часов напомнить о сроке (через запятую)",
  renewal_reminder_hours:
    "За сколько часов напомнить о продлении (через запятую)",
  media_max_bytes: "Лимит медиафайла, байт",
  retention_days: "Хранение файлов, дней",
  timezone: "Часовой пояс IANA",
  expiry: "Истечение доступа (block)",
  trial_hours: "Trial, часов",
  min_seconds: "Минимальная длительность комплекса, секунд",
  max_seconds: "Максимальная длительность комплекса, секунд",
  lessons: "Уроки",
  modules: "Модули курса",
  lesson_ids: "Уроки модуля, через запятую",
  exercise_ids: "Версии упражнений урока, через запятую",
  requires: "Предыдущие уроки, через запятую",
  format: "Формат",
  filename: "Имя PDF-файла",
  needs_video: "Нужно видео",
  model: "Модель",
  max_output_tokens: "Лимит токенов ответа",
  daily_jobs: "Лимит ИИ-заданий в сутки",
  allow_external_ai: "Разрешена передача текста ИИ-провайдеру",
};
const templates = {
  funnel: {},
  onboarding: { age_group: "3-4", concerns: [] },
  decision: { owner: "", status: "open", epics: [], evidence: "" },
  material: {
    owner: "",
    status: "missing",
    source: "",
    format: "",
    rights: "",
    needs_video: true,
    text: "",
    filename: "material.pdf",
  },
  method: {
    audience: "child",
    min_months: 0,
    max_months: 0,
    source: "",
    rights: "",
    limitations: "",
    questions: [],
    bands: [],
    examples: [],
  },
  product: {
    material_id: "",
    age_group: "",
    service: "pdf",
    amount: 0,
    currency: "XTR",
    period_hours: 0,
    terms: "",
    launch_approved: false,
    web_sales_approved: false,
    lava_offer_id: "",
    required_media: true,
    required_fields: [],
    case_questions: [],
    course_id: "",
  },
  exercise: {
    source: "",
    rights: "",
    instructions: "",
    duration_seconds: 0,
    min_months: 36,
    max_months: 132,
    difficulty: 0,
    equipment: [],
    exclusions: [],
    video_id: "",
    video_verified: false,
    alternatives: [],
  },
  selection: {
    source: "",
    instructions: "",
    min_seconds: 600,
    max_seconds: 900,
    difficulty: 0,
    examples: [],
    allowed_difficulties: [],
    adaptation: { easy: 0, suitable: 0, hard: 0, stop: 0 },
  },
  course: {
    source: "",
    rights: "",
    instructions: "",
    audience: "child",
    min_months: 0,
    max_months: 0,
    lessons: [],
    modules: [],
  },
  template: { type: "report", text: "", source: "" },
};
const policyTemplates = {
  commerce: {
    type: "commerce",
    terms: "",
    family_at: "quote",
    include_future: false,
    rank: "first_paid_then_created",
    rounding: "half_up",
    trial_purchase: "",
    quote_ttl_seconds: 0,
    renewal_window_hours: 0,
    max_future_periods: 0,
    late_payment: "review",
    refund_policy_approved: false,
  },
  operations: {
    type: "operations",
    terms: "",
    pause_sales: true,
    max_open_orders: 0,
    sla_reminder_hours: [],
    renewal_reminder_hours: [],
  },
  privacy: {
    type: "privacy",
    terms: "",
    media_max_bytes: 0,
    retention_days: 0,
    allow_external_ai: false,
  },
  training: {
    type: "training",
    terms: "",
    timezone: "Europe/Moscow",
    expiry: "block",
    trial_hours: 72,
  },
  ai: { type: "ai", terms: "", model: "", max_output_tokens: 0, daily_jobs: 0 },
};
const itemTemplates = {
  concerns: { id: "", text: "", description: "", activities: [],
    observation_focus: "", activity_note: "", activity_source: "" },
  activities: { id: "", title: "", instructions: "" },
  case_questions: { id: "", text: "", required: true },
  questions: {
    id: "",
    text: "",
    required: true,
    options: [],
    when: { question: "", answer: "" },
  },
  options: { id: "", text: "", score: 0 },
  bands: { min: 0, max: 0, text: "" },
  guide_pages: { title: "", text: "" },
  followups: { hours: 24, text: "" },
  examples: { answers: {}, score: 0, text: "" },
  modules: { id: "", title: "", lesson_ids: [], requires: [] },
  lessons: { id: "", title: "", text: "", requires: [], exercise_ids: [] },
};
const clone = (v) => JSON.parse(JSON.stringify(v));
function structuredEditor(data, context = "") {
  const box = el("div"),
    getters = {};
  for (const [name, rawValue] of Object.entries(data)) {
    const value =
      name === "when" && !rawValue ? { question: "", answer: "" } : rawValue;
    if (["approval", "previous_version"].includes(name)) continue;
    const title = context === "guide_pages" && name === "title" ? "Заголовок страницы"
      : context === "activities" && name === "title" ? "Название задания"
      : context === "activities" && name === "instructions" ? "Инструкция задания"
      : labels[name] || name;
    if (
      name === "answers" &&
      typeof value === "object" &&
      !Array.isArray(value)
    ) {
      const f = field(
        title,
        "textarea",
        Object.entries(value)
          .map(([k, v]) => k + "=" + v)
          .join("\n"),
      );
      box.append(f.node);
      getters[name] = () =>
        Object.fromEntries(
          f
            .get()
            .split("\n")
            .filter((s) => s.trim())
            .map((line) => line.split("=").map((s) => s.trim())),
        );
    } else if (Array.isArray(value) && itemTemplates[name]) {
      const area = el("fieldset", {}, el("legend", {}, title));
      const items = [];
      function add(v) {
        const editor = structuredEditor(
            { ...clone(itemTemplates[name]), ...v },
            name,
          ),
          row = el("div", { class: "array-item" }, editor.node);
        const item = { row, get: editor.get };
        row.append(
          button(
            "Удалить",
            () => {
              items.splice(items.indexOf(item), 1);
              row.remove();
            },
            true,
          ),
        );
        items.push(item);
        area.insertBefore(row, addButton);
      }
      const addButton = button(
        "Добавить",
        () => add(clone(itemTemplates[name])),
        true,
      );
      area.append(addButton);
      value.forEach(add);
      box.append(area);
      getters[name] = () => items.map((i) => i.get());
    } else if (Array.isArray(value)) {
      const f = field(title, "text", value.join(", "));
      box.append(f.node);
      getters[name] = () =>
        f
          .get()
          .split(",")
          .map((v) => v.trim())
          .filter(Boolean)
          .map((v) =>
            name.endsWith("_hours") || name === "allowed_difficulties"
              ? Number(v)
              : v,
          );
    } else if (value && typeof value === "object") {
      const editor = structuredEditor(value, name);
      box.append(el("fieldset", {}, el("legend", {}, title), editor.node));
      getters[name] =
        name === "when"
          ? () => {
              const value = editor.get();
              return value.question ? value : null;
            }
          : editor.get;
    } else {
      const f = field(
        title,
        typeof value === "boolean"
          ? "checkbox"
          : typeof value === "number"
            ? "number"
            : name.endsWith("_text") || [
                  "text",
                  "description",
                  "observation_focus",
                  "activity_note",
                  "instructions",
                  "limitations",
                  "terms",
                  "evidence",
                ].includes(name)
              ? "textarea"
              : "text",
        value,
      );
      box.append(f.node);
      getters[name] = f.get;
    }
  }
  return {
    node: box,
    get: () =>
      Object.fromEntries(Object.entries(getters).map(([k, get]) => [k, get()])),
  };
}
async function adminContent() {
  const kind = selectField(
    "Раздел",
    Object.entries(kinds).filter(([k]) => templates[k] || k === "policy"),
  );
  const search = field("Поиск");
  const list = el("div", { class: "stack" });
  const load = async () => {
    const data = await api(
      "/admin/content?kind=" +
        kind.get() +
        "&q=" +
        encodeURIComponent(search.get()),
    );
    list.replaceChildren(
      ...(data.items.length
        ? data.items.map((o) =>
            el(
              "article",
              { class: "card" },
              el(
                "div",
                { class: "row between" },
                el("h3", {}, o.data.title),
                badge(o.status),
              ),
              el(
                "small",
                {},
                "Версия " + o.revision + " · " + dateTime(o.updated),
              ),
              el(
                "div",
                { class: "spacer" },
                button("Открыть редакцию", () => editContent(o), true),
              ),
            ),
          )
        : [empty("Пока нет материалов. Создайте первый черновик.")]),
    );
  };
  kind.input.onchange = () => safe(load);
  main(
    heading(
      "Контент и настройки",
      "Новые материалы остаются черновиками до проверки и утверждения специалистом.",
    ),
    el(
      "div",
      { class: "row toolbar" },
      kind.node,
      search.node,
      button("Найти", load, true),
      button("Создать черновик", () => editContent(null, kind.get())),
      button("Профиль Telegram", editFunnelProfile, true),
    ),
    list,
  );
  await load();
}

async function adminTripwire() {
  const data = await api("/admin/tripwire");
  const integrationRows = Object.entries({
    "API Lava.top": data.integrations.lava_api,
    "Ключ webhook Lava.top": data.integrations.lava_webhook,
    "Публичный HTTPS-адрес": data.integrations.public_https,
    "Тестовый внешний checkout": data.integrations.external_lava_test_mode,
  }).map(([title, ready]) =>
    el(
      "div",
      { class: "row between" },
      el("span", {}, title),
      badge(ready ? "active" : "missing"),
    ),
  );
  const openContent = async (id) => editContent(await api("/admin/content/" + id));
  const groups = data.groups.map((group) => {
    const funnel = group.funnel,
      product = group.product,
      material = group.material;
    return el(
      "article",
      { class: "card" },
      el("h2", {}, group.label),
      funnel
        ? el(
            "section",
            {},
            el("h3", {}, "Сценарий"),
            badge(funnel.status),
            el(
              "p",
              { class: "quiet" },
              "Редакция " + funnel.revision +
                (funnel.contact_configured ? " · контакт настроен" : " · нет контакта"),
            ),
            button("Открыть сценарий", () => openContent(funnel.id), true),
          )
        : el("p", { class: "overdue" }, "Опубликованный сценарий отсутствует"),
      product
        ? el(
            "section",
            {},
            el("h3", {}, "Тариф"),
            badge(product.status),
            el(
              "p",
              {},
              String(product.amount) + " " + product.currency +
                (product.lava_offer_configured ? " · Lava offer настроен" : " · нет Lava offer"),
            ),
            button("Открыть тариф", () => openContent(product.id), true),
          )
        : el("p", { class: "overdue" }, "Тариф не привязан"),
      material
        ? el(
            "section",
            {},
            el("h3", {}, "PDF-материал"),
            badge(material.status),
            el(
              "p",
              { class: material.asset_configured ? "quiet" : "overdue" },
              material.asset_configured ? "PDF прикреплён" : "PDF не сформирован",
            ),
            button("Открыть материал", () => openContent(material.id), true),
          )
        : el("p", { class: "overdue" }, "Материал не привязан"),
    );
  });
  main(
    heading(
      "Трипваер",
      "Материалы, тарифы, сценарии и платёжные состояния управляются как отдельные версионируемые сущности.",
    ),
    el(
      "section",
      { class: "card" },
      el("h2", {}, "Готовность Lava.top"),
      ...integrationRows,
      el(
        "p",
        { class: "quiet" },
        "Внешняя оплата цифрового PDF из Telegram разрешается только на dev для ручного теста. Секреты хранятся в окружении и здесь не отображаются.",
      ),
    ),
    el(
      "div",
      { class: "row toolbar" },
      button("Создать PDF из текста", () => editContent(null, "material")),
      button("Создать тариф", () => editContent(null, "product"), true),
      button("Все сущности и операции", () => adminObjects(), true),
    ),
    el("div", { class: "stack spacer" }, ...groups),
    el(
      "section",
      { class: "card" },
      el("h2", {}, "Состояния сущностей"),
      details(data.entities),
    ),
  );
}
async function editContent(obj, kind) {
  kind = obj?.kind || kind;
  let template = clone(templates[kind] || policyTemplates.commerce);
  if (kind === "funnel" && !obj) {
    const group = selectField("Возрастная группа скрининга", [
      ["3-4", "3–4 года"], ["5-6", "5–6 лет"],
      ["7-9", "7–9 лет"], ["10-14", "10–14 лет"],
    ]);
    modal(form("Сценарий из документа владельца", [group], "Продолжить", async () => {
      const defaults = await api("/admin/funnel-defaults/" + group.get());
      $("#dialog").close();
      await contentForm(null, kind, defaults);
    }));
    return;
  }
  if (kind === "onboarding" && !obj) {
    const group = selectField("Возрастная группа", [
      ["3-4", "3–4 года"], ["5-6", "5–6 лет"],
      ["7-8", "7–8 лет"], ["9-10", "9–10 лет"],
    ]);
    modal(form("Выберите экран знакомства", [group], "Продолжить", async () => {
      const defaults = await api("/admin/onboarding-defaults/" + group.get());
      $("#dialog").close();
      await contentForm(null, kind, defaults);
    }));
    return;
  }
  if (kind === "policy" && !obj) {
    const type = selectField(
      "Тип правил",
      Object.keys(policyTemplates).map((k) => [k, k]),
    );
    modal(
      form("Выберите тип правил", [type], "Продолжить", async () => {
        $("#dialog").close();
        await contentForm(null, kind, clone(policyTemplates[type.get()]));
      }),
    );
    return;
  }
  if (obj) template = { ...template, ...obj.data };
  await contentForm(obj, kind, template);
}
async function contentForm(obj, kind, template) {
  const title = field("Название", "text", obj?.data.title || template.title || "");
  delete template.title;
  const editor = structuredEditor(template),
    why = field("Основание / причина действия", "textarea");
  const f = form(
    (obj ? "Редакция: " : "Новый материал: ") + kinds[kind],
    [title, { node: editor.node }, why],
    "Сохранить черновик",
    async () => {
      const payload = {
        title: title.get(),
        data: editor.get(),
        reason: why.get(),
      };
      if (obj) payload.revision = obj.revision;
      const result = await api(
        "/admin/content" + (obj ? "/" + obj.id : ""),
        obj ? "PATCH" : "POST",
        obj ? payload : { ...payload, kind },
      );
      notice("Черновик сохранён");
      await editContent(result);
    },
  );
  if (obj && obj.status !== "draft")
    f.querySelector("button[type=submit]").disabled = true;
  if (obj) {
    if (kind === "funnel") {
      f.append(button("Скачать PDF для проверки", async () => {
        const url = await authenticatedFile("/admin/funnels/" + obj.id + "/guide-preview");
        el("a", { href: url, download: obj.data.guide_filename }).click();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
      }, true));
      f.append(button("Подготовить PDF как черновик", async () => {
        const material = await api("/admin/funnels/" + obj.id + "/prepare-guide", "POST", {
          revision: obj.revision, reason: why.get(),
        });
        notice("PDF сохранён в материалах как черновик. Проверьте и утвердите его перед продажей.");
        await editContent(material);
      }, true));
    }
    f.prepend(
      button(
        "Предпросмотр и проверка",
        async () =>
          modal(details(await api("/admin/content/" + obj.id + "/preview"))),
        true,
      ),
    );
    f.prepend(
      badge(obj.status),
      el(
        "p",
        { class: "quiet" },
        "ID: " + obj.id + " · редакция " + obj.revision,
      ),
    );
    const actions = el("div", { class: "row spacer" });
    const allowed =
      obj.status === "draft"
        ? ["approve", "copy", "archive"]
        : obj.status === "approved"
          ? ["publish", "copy", "archive"]
          : ["copy", "archive"];
    for (const action of allowed)
      actions.append(
        button(
          {
            approve: "Утвердить эту редакцию",
            publish: "Опубликовать",
            copy: "Новая версия на основе этой",
            archive: "Архивировать",
          }[action],
          async () => {
            const result = await api(
              "/admin/content/" + obj.id + "/" + action,
              "POST",
              { revision: obj.revision, reason: why.get() },
            );
            notice("Действие сохранено");
            await editContent(result);
          },
          action !== "publish",
        ),
      );
    f.append(
      actions,
      button("История изменений", () => auditPage(obj.id), true),
    );
    if (kind === "exercise" || kind === "material") {
      if (kind === "material" && obj.data.asset_id) {
        f.append(button("Скачать PDF материала", async () => {
          const url = await authenticatedFile("/admin/materials/" + obj.id + "/pdf");
          el("a", { href: url, download: "guide.pdf" }).click();
          setTimeout(() => URL.revokeObjectURL(url), 30000);
        }, true));
      }
      if (kind === "material" && obj.data.text) {
        f.append(button("Сформировать PDF из текста", async () => {
          const rendered = await api("/admin/materials/" + obj.id + "/render-pdf", "POST", {
            revision: obj.revision,
            reason: why.get(),
          });
          notice("PDF сформирован и прикреплён к черновику.");
          await editContent(rendered);
        }, true));
      }
      const file = field("Видео MP4 / материал", "file");
      f.append(
        el(
          "fieldset",
          {},
          el("legend", {}, "Загрузка исходника"),
          file.node,
          button(
            "Сохранить файл",
            async () => {
              const fd = new FormData();
              fd.append("file", file.input.files[0]);
              fd.append("reason", why.get());
              const asset = await api(
                "/admin/content/" + obj.id + "/asset",
                "POST",
                fd,
              );
              notice("Файл сохранён: " + asset.id);
              if (kind === "exercise") {
                const updated = {
                  ...obj,
                  data: {
                    ...obj.data,
                    video_id: asset.id,
                    video_verified: false,
                    alternatives: [],
                  },
                };
                await editContent(updated);
              } else if (kind === "material") {
                await editContent({ ...obj, data: { ...obj.data, asset_id: asset.id } });
              }
            },
            true,
          ),
        ),
      );
    }
  }
  main(f);
}

async function editFunnelProfile() {
  const current = await api("/admin/funnel-profile");
  const name = field("Имя бота", "text", current.name);
  const description = field("Описание до нажатия Старт", "textarea", current.description);
  const why = field("Основание / причина действия", "textarea");
  modal(form("Профиль Telegram", [name, description, why], "Применить в Telegram", async () => {
    await api("/admin/funnel-profile", "POST", { name: name.get(), description: description.get(), reason: why.get() });
    $("#dialog").close();
    notice("Обновление профиля поставлено в очередь. Результат виден в истории операций.");
  }));
}
async function adminQueue() {
  const data = await api("/admin/queue");
  main(
    heading(
      "Очередь специалиста",
      "Исходные сроки сохраняются при смене статуса и повторной обработке.",
    ),
    el(
      "div",
      { class: "stack" },
      ...(data.items.length
        ? data.items.map((o) =>
            el(
              "article",
              { class: "card" },
              el(
                "div",
                { class: "row between" },
                el("h3", {}, kinds[o.kind]),
                badge(o.status),
              ),
              el("p", {}, o.subject ? "Профиль: " + o.subject : "Общая задача"),
              el(
                "p",
                { class: o.overdue ? "badge overdue" : "quiet" },
                o.due_at
                  ? (o.overdue ? "Просрочено · " : "Срок · ") +
                      dateTime(o.due_at)
                  : "Срок пока не запущен",
              ),
              button("Открыть карточку", () => adminCard(o.id)),
            ),
          )
        : [empty("Очередь пуста.")]),
    ),
  );
}
function details(value) {
  if (value == null) return el("span", {}, "—");
  if (typeof value !== "object")
    return el("span", { class: "readable" }, String(value));
  if (Array.isArray(value))
    return el("div", { class: "stack" }, ...value.map(details));
  return el(
    "dl",
    {},
    ...Object.entries(value)
      .filter(
        ([k]) =>
          ![
            "approval",
            "method_snapshot",
            "policy_snapshot",
            "product_snapshot",
            "rule_snapshot",
          ].includes(k),
      )
      .flatMap(([k, v]) => [
        el("dt", { class: "quiet" }, labels[k] || k),
        el(
          "dd",
          {},
          typeof v === "string" &&
            [
              "order_id",
              "attempt_id",
              "report_id",
              "product_id",
              "course_id",
              "template_id",
              "policy_id",
              "media_id",
              "case_id",
              "payment_event_id",
              "refund_id",
            ].includes(k)
            ? button("Открыть " + v, () => adminCard(v), true)
            : details(v),
        ),
      ]),
  );
}
async function adminCard(id) {
  const card = await api("/admin/objects/" + id),
    o = card.object;
  if (o.kind === "video_review") return adminCrmCard(id);
  if (templates[o.kind] || o.kind === "policy") return editContent(o);
  if (o.kind === "subject") return editAdminSubject(o);
  const panel = el(
    "div",
    { class: "stack" },
    heading(kinds[o.kind] || o.kind),
    el(
      "div",
      { class: "row" },
      o.owner
        ? button("Аккаунт владельца", () => adminAccount(o.owner), true)
        : null,
      o.subject
        ? button("Карточка профиля", () => adminCard(o.subject), true)
        : null,
    ),
    el(
      "div",
      { class: "card" },
      badge(o.status),
      el("p", { class: "quiet" }, "ID: " + id + " · версия " + o.revision),
      details(o.data),
    ),
  );
  const why = field("Причина действия", "textarea"),
    text = field(
      "Текст ответа / новой редакции",
      "textarea",
      o.kind === "report" ? o.data.revisions?.slice(-1)[0]?.text || "" : "",
    ),
    actions = el("div", { class: "card stack" }, why.node, text.node);
  const command = async (path, data = {}) => {
    await api(path, "POST", {
      revision: o.revision,
      reason: why.get(),
      text: text.get(),
      ...data,
    });
    notice("Действие сохранено");
    await adminCard(id);
  };
  if (o.kind === "funnel_run" || o.kind === "funnel_entry") {
    actions.append(button("Остановить напоминания", () => command("/admin/" + (o.kind === "funnel_run" ? "funnel-runs/" : "funnel-entries/") + id + "/stop"), true));
    if (o.data.booking_id)
      actions.append(button("Заявка на диагностику", () => adminCard(o.data.booking_id), true));
  }
  if (o.kind === "report") {
    const n = o.data.revisions?.length || 0;
    actions.append(
      button("Сохранить новую редакцию", () =>
        command("/admin/reports/" + id + "/revise"),
      ),
      button(
        "Утвердить текущую редакцию",
        () =>
          command("/admin/reports/" + id + "/approve", { text_revision: n }),
        true,
      ),
      button("Собрать и опубликовать PDF", () =>
        command("/admin/reports/" + id + "/publish", { text_revision: n }),
      ),
    );
  }
  if (o.kind === "case") {
    const attachmentIds = [];
    const responseFile = field(
      "Приложение к ответу (PDF, MP4, MP3, OGG)",
      "file",
    );
    actions.append(
      responseFile.node,
      button(
        "Загрузить приложение ответа",
        async () => {
          const data = new FormData();
          data.append("file", responseFile.input.files[0]);
          data.append("reason", why.get());
          const file = await api("/admin/cases/" + id + "/media", "POST", data);
          attachmentIds.push(file.id);
          notice("Приложение сохранено. Теперь опубликуйте основной ответ.");
        },
        true,
      ),
    );
    actions.append(
      button("Опубликовать основной ответ", () =>
        command("/admin/cases/" + id + "/answer", {
          attachments: attachmentIds,
        }),
      ),
      button(
        "Опубликовать исправление основного ответа",
        () => command("/admin/cases/" + id + "/correct-answer"),
        true,
      ),
      button("Ответить на уточнение", () =>
        command("/admin/cases/" + id + "/followup-answer"),
      ),
      button(
        "Заметка / запрос сведений",
        () => command("/admin/cases/" + id + "/request-info"),
        true,
      ),
    );
  }
  if (o.kind === "booking") {
    const time = field(
        "Дата и время в часовом поясе браузера",
        "datetime-local",
      ),
      duration = field("Длительность, минут", "number", 60);
    actions.append(
      time.node,
      duration.node,
      button("Предложить время", () =>
        command("/admin/bookings/" + id + "/propose", {
          starts: Math.floor(new Date(time.get()).getTime() / 1000),
          duration_minutes: duration.get(),
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        }),
      ),
      button("Сохранить HTTPS-ссылку встречи", () =>
        command("/admin/bookings/" + id + "/connection"),
      ),
      button(
        "Завершить встречу",
        () => command("/admin/bookings/" + id + "/complete"),
        true,
      ),
      button(
        "Отменить",
        () => command("/admin/bookings/" + id + "/cancel"),
        true,
      ),
    );
  }
  if (o.kind === "reconciliation")
    actions.append(
      button("Зафиксировать решение", () =>
        command("/admin/reconciliations/" + id + "/resolve"),
      ),
    );
  if (o.kind === "right")
    actions.append(
      button("Отозвать это право", () =>
        command("/admin/rights/" + id + "/revoke"),
      ),
    );
  if (o.kind === "payment_attempt")
    actions.append(
      button("Сверить Lava.top", () =>
        command("/admin/payments/" + id + "/reconcile"),
      ),
    );
  if (card.media?.length)
    panel.append(
      el(
        "section",
        { class: "card" },
        el("h2", {}, "Файлы"),
        ...card.media.map((file) =>
          button(
            "Открыть " + file.mime,
            async () => {
              const src = await authenticatedFile("/media/" + file.id);
              window.open(src, "_blank", "noopener");
            },
            true,
          ),
        ),
      ),
    );
  if (o.kind === "report") {
    const templates = (await api("/admin/content?kind=template")).items.filter(
      (t) => t.status === "published" && t.data.type === "report",
    );
    const template = selectField(
      "Шаблон ИИ-черновика",
      templates.map((t) => [t.id, t.data.title]),
    );
    actions.append(
      template.node,
      button(
        "Подготовить текстовый ИИ-черновик",
        () =>
          command("/admin/reports/" + id + "/ai-draft", {
            template_id: template.get(),
          }),
        true,
      ),
    );
  }
  if (o.kind === "order" && card.receipts.length) {
    const charge = selectField(
      "Подтверждённое поступление для полного возврата",
      card.receipts.map((r) => [
        r.charge,
        r.provider + " · " + r.amount + " " + r.currency,
      ]),
    );
    actions.append(
      charge.node,
      button(
        "Запросить полный возврат Stars",
        async () => {
          const receipt = card.receipts.find((r) => r.charge === charge.get());
          await command("/admin/orders/" + id + "/refund", {
            charge: receipt.charge,
            amount: receipt.amount,
          });
        },
        true,
      ),
    );
  }
  if (o.kind === "data_request")
    actions.append(
      ...["in_progress", "awaiting_policy", "reject"].map((action) =>
        button(
          {
            in_progress: "Взять запрос в работу",
            awaiting_policy: "Ожидает утверждения политики",
            reject: "Отклонить с причиной",
          }[action],
          () => command("/admin/data-requests/" + id + "/" + action),
          true,
        ),
      ),
    );
  if (o.kind === "retention_task")
    actions.append(
      button(
        "Удалить файл по истёкшей политике",
        async () => {
          const media = (await api("/admin/objects/" + o.data.media_id)).object;
          await api("/admin/media/" + media.id + "/delete", "POST", {
            revision: media.revision,
            reason: why.get(),
          });
          await adminQueue();
        },
        true,
      ),
    );
  if (o.kind === "ai_job")
    actions.append(
      button(
        "Отменить ожидающее задание",
        () => command("/admin/jobs/" + id + "/cancel"),
        true,
      ),
    );
  panel.append(
    actions,
    o.kind === "plan"
      ? button("Заменить упражнение будущего плана", () => replacePlan(o), true)
      : null,
    el(
      "section",
      { class: "card" },
      el("h2", {}, "Оплата и исполнение"),
      details(card.receipts),
      ...card.fulfillments.map((f) =>
        button(
          "Открыть результат " + f.line_id,
          () => adminCard(f.object_id),
          true,
        ),
      ),
    ),
    button("Аудит объекта", () => auditPage(id), true),
    button(
      "История редакций",
      async () =>
        modal(details((await api("/admin/objects/" + id + "/versions")).items)),
      true,
    ),
  );
  main(panel);
}
async function adminAccounts() {
  const q = field("Имя или ID"),
    list = el("div", { class: "stack" });
  const load = async () => {
    const data = await api("/admin/accounts?q=" + encodeURIComponent(q.get()));
    list.replaceChildren(
      ...data.items.map((a) =>
        el(
          "article",
          { class: "card" },
          el("h3", {}, a.name),
          el("p", { class: "quiet" }, a.id),
          button(
            "Открыть клиента",
            async () => {
              const data = await api("/admin/accounts/" + a.id),
                why = field("Причина действия", "textarea");
              main(
                heading(a.name),
                el("p", {}, "Telegram ID: " + a.telegram_id),
                why.node,
                el(
                  "div",
                  { class: "row" },
                  ...["block", "restore", "revoke-sessions"].map((action) =>
                    button(
                      {
                        block: "Ограничить вход",
                        restore: "Восстановить вход",
                        "revoke-sessions": "Отозвать сессии",
                      }[action],
                      async () => {
                        await api(
                          "/admin/accounts/" + a.id + "/" + action,
                          "POST",
                          { reason: why.get() },
                        );
                        notice("Действие сохранено");
                      },
                      true,
                    ),
                  ),
                ),
                el("h2", { class: "spacer" }, "Профили и обращения"),
                el(
                  "div",
                  { class: "stack" },
                  ...data.objects.map((o) =>
                    el(
                      "div",
                      { class: "card" },
                      el("h3", {}, o.data.name || kinds[o.kind] || o.kind),
                      badge(o.status),
                      o.kind === "subject"
                        ? el(
                            "div",
                            {},
                            details(o.data),
                            button(
                              "Изменить профиль",
                              () => editAdminSubject(o),
                              true,
                            ),
                            button(
                              "Оформить компенсационный доступ",
                              () => compensate(o),
                              true,
                            ),
                          )
                        : button("Открыть", () => adminCard(o.id), true),
                    ),
                  ),
                ),
              );
            },
            true,
          ),
        ),
      ),
    );
  };
  main(heading("Клиенты"), q.node, button("Найти", load), list);
  await load();
}
async function operations() {
  const data = await api("/admin/operations");
  main(
    heading(
      "Эксплуатация",
      "Состояния интеграций, очередь доставки и продуктовые счётчики.",
    ),
    el(
      "div",
      { class: "grid" },
      ...Object.entries(data.integrations).map(([k, v]) =>
        el(
          "div",
          { class: "card" },
          el("h3", {}, k),
          badge(v ? "ready" : "draft"),
        ),
      ),
    ),
    el("h2", { class: "spacer" }, "События доставки"),
    el(
      "div",
      { class: "stack" },
      ...data.outbox.map((e) =>
        el(
          "article",
          { class: "card" },
          el("h3", {}, e.event),
          badge(e.status),
          el(
            "p",
            { class: "quiet" },
            "Попыток: " + e.attempts + " · " + dateTime(e.created),
          ),
          button("Открыть исходный объект", () => adminCard(e.object_id), true),
          button(
            "История попыток",
            async () =>
              modal(
                details(
                  (await api("/admin/outbox/" + e.id + "/attempts")).items,
                ),
              ),
            true,
          ),
          e.event !== "invoice.create" && ["sent", "failed"].includes(e.status)
            ? button(
                "Повторить доставку",
                async () => {
                  const why = field("Причина повтора", "textarea");
                  modal(
                    form("Повтор доставки", [why], "Повторить", async () => {
                      await api("/admin/outbox/" + e.id + "/retry", "POST", {
                        reason: why.get(),
                      });
                      $("#dialog").close();
                      await operations();
                    }),
                  );
                },
                true,
              )
            : null,
        ),
      ),
    ),
    el(
      "section",
      { class: "card spacer" },
      el("h2", {}, "Счётчики"),
      details(data.metrics),
    ),
  );
}
async function auditPage(id) {
  const data = await api("/admin/audit" + (id ? "?object_id=" + id : ""));
  const table = el(
    "table",
    {},
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        ...["Время", "Действие", "Объект", "Исполнитель", "Причина"].map((t) =>
          el("th", {}, t),
        ),
      ),
    ),
    el(
      "tbody",
      {},
      ...data.items.map((a) =>
        el(
          "tr",
          {},
          ...[dateTime(a.at), a.action, a.object_id, a.actor, a.reason].map(
            (t) => el("td", {}, t),
          ),
        ),
      ),
    ),
  );
  main(heading("Журнал аудита"), table);
}

async function editAdminSubject(o) {
  const name = field("Имя", "text", o.data.name),
    birth = field("Дата рождения", "date", o.data.birth_date),
    archive = field(
      "Архивировать профиль",
      "checkbox",
      o.status === "archived",
    ),
    why = field("Причина", "textarea");
  modal(
    form(
      "Профиль клиента",
      [name, birth, archive, why],
      "Сохранить",
      async () => {
        await api("/admin/subjects/" + o.id, "POST", {
          revision: o.revision,
          name: name.get(),
          birth_date: birth.get(),
          archive: archive.get(),
          reason: why.get(),
        });
        $("#dialog").close();
        await adminAccounts();
      },
    ),
  );
  $("#dialog-body").append(
    button(
      "Подготовить будущий комплекс",
      async () => {
        $("#dialog").close();
        await prepareAdminPlan(o);
      },
      true,
    ),
  );
}
async function compensate(subject) {
  const service = selectField("Вид доступа", [
      ["subscription", "Занятия"],
      ["course", "Курс"],
    ]),
    course = field("ID версии курса (для курса)"),
    start = field("Начало периода", "datetime-local"),
    end = field("Конец периода", "datetime-local"),
    why = field("Причина компенсации", "textarea");
  modal(
    form(
      "Отдельное компенсационное право",
      [service, course, start, end, why],
      "Выдать право",
      async () => {
        await api("/admin/rights/grant", "POST", {
          subject_id: subject.id,
          service: service.get(),
          course_id: course.get() || null,
          starts: Math.floor(new Date(start.get()).getTime() / 1000),
          ends: Math.floor(new Date(end.get()).getTime() / 1000),
          reason: why.get(),
          key: key(),
        });
        $("#dialog").close();
        notice("Отдельное право оформлено. Trial и оплаты сохранены.");
      },
    ),
  );
}

async function prepareAdminPlan(subject) {
  const equipment = field("Доступные предметы, через запятую"),
    constraints = field("Ограничения, через запятую");
  modal(
    form(
      "Будущий комплекс: " + subject.data.name,
      [equipment, constraints],
      "Подобрать и сохранить",
      async () => {
        const plan = await api("/admin/plans", "POST", {
          subject_id: subject.id,
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
        $("#dialog").close();
        await adminCard(plan.id);
      },
    ),
  );
}
async function replacePlan(plan) {
  const list = (await api("/admin/content?kind=exercise")).items.filter(
    (o) => o.status === "published",
  );
  const original = selectField(
    "Упражнение для замены",
    plan.data.exercises.map((e) => [e.id, e.title]),
  );
  const replacement = selectField(
    "Разрешённая альтернатива",
    list.map((e) => [e.id, e.data.title]),
  );
  const why = field("Причина замены", "textarea");
  modal(
    form(
      "Замена будущего назначения",
      [original, replacement, why],
      "Проверить и заменить",
      async () => {
        await api("/admin/plans/" + plan.id + "/replace", "POST", {
          revision: plan.revision,
          exercise_id: original.get(),
          replacement_id: replacement.get(),
          reason: why.get(),
        });
        $("#dialog").close();
        await adminCard(plan.id);
      },
    ),
  );
}

async function adminObjects(offset = 0) {
  const kind = selectField("Тип объекта", [
      ["", "Все типы"],
      ...Object.entries(kinds),
    ]),
    status = selectField("Состояние", [
      ["", "Любое состояние"],
      ...Object.entries(states),
    ]);
  const list = el("div", { class: "stack" });
  const load = async (page = 0) => {
    const data = await api(
      "/admin/objects?kind=" +
        encodeURIComponent(kind.get()) +
        "&status=" +
        encodeURIComponent(status.get()) +
        "&offset=" +
        page,
    );
    list.replaceChildren(
      ...data.items.map((o) =>
        el(
          "article",
          { class: "card" },
          el("h3", {}, o.data.title || o.data.name || kinds[o.kind] || o.kind),
          badge(o.status),
          el("p", { class: "quiet" }, dateTime(o.updated)),
          button("Открыть карточку", () => adminCard(o.id), true),
        ),
      ),
      el(
        "div",
        { class: "row" },
        page > 0
          ? button("Назад", () => load(Math.max(0, page - 100)), true)
          : null,
        data.items.length === 100
          ? button("Далее", () => load(page + 100), true)
          : null,
      ),
    );
  };
  kind.input.onchange = () => safe(() => load());
  status.input.onchange = () => safe(() => load());
  main(
    heading("Все объекты"),
    el("div", { class: "row" }, kind.node, status.node),
    list,
  );
  await load(offset);
}
async function adminAccount(id) {
  const data = await api("/admin/accounts/" + id);
  main(
    heading(data.account.name),
    el(
      "section",
      { class: "card" },
      el("h2", {}, "Учётная запись"),
      details({
        telegram_id: data.account.telegram_id,
        blocked: Boolean(data.account.blocked),
        trial: data.trial,
        "Первое знакомство": data.introduction,
        sessions: data.sessions,
      }),
    ),
    el(
      "div",
      { class: "stack spacer" },
      ...data.objects.map((o) =>
        el(
          "article",
          { class: "card" },
          el("h3", {}, o.data.title || o.data.name || kinds[o.kind] || o.kind),
          badge(o.status),
          button("Открыть", () => adminCard(o.id), true),
        ),
      ),
    ),
  );
}
async function analyticsPage() {
  const from = field("Начало периода", "date"),
    to = field("Конец периода (не включая)", "date");
  const result = el("div", { class: "stack spacer" });
  const load = async () => {
    const params = new URLSearchParams();
    if (from.get())
      params.set("from", Math.floor(new Date(from.get()).getTime() / 1000));
    if (to.get())
      params.set("to", Math.floor(new Date(to.get()).getTime() / 1000));
    const data = await api("/admin/analytics?" + params);
    result.replaceChildren(
      el(
        "section",
        { class: "card" },
        el("h2", {}, "Переходы воронки"),
        details(data.funnel),
      ),
      el(
        "section",
        { class: "card" },
        el("h2", {}, "Поступления по валютам и состояниям"),
        el(
          "p",
          { class: "quiet" },
          "Период по времени поступления события на сервер.",
        ),
        details(data.receipts),
      ),
      el(
        "section",
        { class: "card" },
        el("h2", {}, "Исполнение сроков"),
        details(data.sla),
      ),
      button(
        "Выгрузить отчёт",
        async () => {
          const url = URL.createObjectURL(
            new Blob([JSON.stringify(data, null, 2)], {
              type: "application/json",
            }),
          );
          const a = el("a", { href: url, download: "operations-report.json" });
          a.click();
          setTimeout(() => URL.revokeObjectURL(url), 5000);
        },
        true,
      ),
    );
  };
  main(
    heading("Операционные отчёты"),
    el(
      "div",
      { class: "row" },
      from.node,
      to.node,
      button("Показать период", load),
    ),
    result,
  );
  await load();
}

async function adminCrm() {
  const initial = await api('/admin/crm');
  const state = selectField('Статус CRM', [['', 'Все статусы'], ...Object.entries(initial.states)]);
  const specialist = selectField('Ответственный специалист', [['', 'Все специалисты'], ...initial.specialists.map(s => [s.id, s.name])]);
  const search = field('Поиск клиента или проблемы');
  const list = el('div', {class: 'stack'});
  async function load() {
    const data = await api('/admin/crm?status='+encodeURIComponent(state.get())+'&assigned_to='+encodeURIComponent(specialist.get())+'&q='+encodeURIComponent(search.get()));
    list.replaceChildren(
      el('p', {class:'quiet'}, data.truncated ? 'Показаны последние 200 карточек. Уточните фильтры.' : 'Карточек: '+data.items.length),
      ...data.items.map(o => el('article', {class:'card'},
        el('h2', {}, o.client_name), badge(data.states[o.status]),
        el('p', {}, o.data.problem),
        el('p', {class:'quiet'}, 'Ответственный: '+(data.specialists.find(s=>s.id===o.data.assigned_to)?.name || 'Не назначен')),
        el('p', {class:o.data.next_contact && o.data.next_contact < Date.now()/1000 ? 'overdue' : 'quiet'},
          'Следующий контакт: '+dateTime(o.data.next_contact)),
        button('Открыть карточку', ()=>adminCrmCard(o.id)))),
    );
  }
  state.input.onchange = () => safe(load);
  specialist.input.onchange = () => safe(load);
  main(heading('CRM', 'Видео → просмотр → мини-заключение → приглашение → запись → диагностика'),
    el('div', {class:'row'}, state.node, specialist.node, search.node, button('Найти', load, true)), list);
  await load();
}

async function adminCrmCard(id) {
  const card = await api('/admin/crm/'+id), o=card.object, d=o.data;
  const why = field('Причина действия', 'textarea');
  const act = async (action, data={}) => {
    await api('/admin/crm/'+id+'/'+action, 'POST', {revision:o.revision, reason:why.get(), ...data});
    await adminCrmCard(id);
  };
  const assigned = selectField('Ответственный специалист', [['','Выберите специалиста'], ...card.specialists.map(s=>[s.id,s.name])], d.assigned_to || '');
  const next = field('Следующий контакт (МСК)', 'datetime-local', d.next_contact ? new Date((d.next_contact+10800)*1000).toISOString().slice(0,16) : '');
  const comment = field('Новый внутренний комментарий', 'textarea');
  const conclusion = field('Мини-заключение специалиста', 'textarea', d.conclusion_draft || '');
  const invitation = field('Приглашение на полную диагностику', 'textarea', '');
  const panel = el('div', {class:'stack'},
    heading('CRM: '+card.account.name, card.states[o.status]),
    el('div', {class:'row'}, button('К CRM', adminCrm, true), button('Карточка клиента', ()=>adminAccount(o.owner), true)),
    el('section', {class:'card'}, el('h2', {}, card.subject.data.name),
      el('p', {}, 'Проблема: '+d.problem),
      el('p', {}, 'Задание: '+(d.activity?.title || 'Свой вариант')),
      el('p', {}, 'Подпись к видео: '+(d.client_note || '—')),
      el('p', {class:'quiet'}, 'Telegram ID: '+card.account.telegram_id)),
    el('section', {class:'card stack'}, why.node, assigned.node,
      button('Назначить ответственного', ()=>act('assign', {assigned_to:assigned.get()})),
      next.node, button('Сохранить дату контакта', ()=>act('next-contact', {next_contact:next.get() ? Date.parse(next.get()+':00+03:00')/1000 : null})),
      comment.node, button('Добавить комментарий', ()=>act('comment', {text:comment.get()}))),
  );
  if (d.video_id) {
    const player=el('video', {controls:'', preload:'metadata', class:'crm-video'});
    let videoUrl;
    const cleanup=new MutationObserver(()=>{if(!player.isConnected){if(videoUrl)URL.revokeObjectURL(videoUrl);cleanup.disconnect();}});
    cleanup.observe($('#main'), {childList:true});
    panel.append(el('section', {class:'card stack'}, el('h2', {}, 'Видео клиента'), player,
      button('Открыть видео', async()=>{if(videoUrl)URL.revokeObjectURL(videoUrl); videoUrl=await authenticatedFile('/media/'+d.video_id); player.src=videoUrl;}),
      button('Скачать видео', async()=>{const src=await authenticatedFile('/media/'+d.video_id);const link=el('a',{href:src,download:'client-video.mp4'});link.click();setTimeout(()=>URL.revokeObjectURL(src),30000);}, true),
      el('p',{class:'quiet'},'Если видео не запускается, скачайте его для просмотра.'),
      button('Взять на просмотр', ()=>act('take')),
      button('Подтверждаю просмотр видео', ()=>act('viewed')),
      el('p', {class:'quiet'}, d.viewed ? 'Просмотр подтверждён: '+dateTime(d.viewed.at) : 'Просмотр ещё не подтверждён')));
  }
  panel.append(el('section', {class:'card stack'}, el('h2', {}, 'Ответ специалиста'), conclusion.node,
    button('Сохранить мини-заключение', ()=>act('conclusion', {text:conclusion.get()})),
    button('Отправить мини-заключение клиенту', ()=>act('send-conclusion')),
    d.conclusion ? el('p', {}, 'Отправлено: '+d.conclusion) : null,
    invitation.node, button('Отправить приглашение на диагностику', ()=>act('invite', {text:invitation.get()})),
    d.invitation ? el('p', {}, 'Приглашение: '+d.invitation) : null,
    d.pending_message_id ? el('p', {}, 'Сообщение в очереди. Статус обновится после подтверждения отправки Telegram.') : null));
  if (card.delivery?.status === 'failed') panel.append(button('Повторить доставку', async()=>{
    await api('/admin/outbox/'+card.delivery.id+'/retry', 'POST', {reason:why.get()}); await adminCrmCard(id);
  }));
  if (d.booking_id) panel.append(button('Открыть запись на диагностику', ()=>adminCard(d.booking_id)));
  panel.append(el('section', {class:'card'}, el('h2', {}, 'Внутренние комментарии'),
    ...d.comments.map(c=>el('p', {}, dateTime(c.at)+' · '+c.text))),
    el('section', {class:'card'}, el('h2', {}, 'История изменений'),
      ...card.audit.map(a=>el('p', {}, dateTime(a.at)+' · '+a.action+' · '+a.reason))),
    button('Обновить карточку', ()=>adminCrmCard(id), true),
    button('Закрыть обращение', ()=>act('close'), true));
  main(panel);
}
