"""Drive the public cita previa form for toma de huellas.

The only step that waits for a person is the captcha, if the site shows one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import Dialog, async_playwright

from fields import has_phrase, is_huellas, is_placeholder, match_label, norm

log = logging.getLogger("cita")

START_URL = "https://icp.administracionelectronica.gob.es/icpplus/index.html"
FORWARD = ("#btnAceptar", "#btnEntrar", "#btnEnviar", "#btnSiguiente", "#btnConfirmar")
NO_CITAS = (
    "en este momento no hay citas",
    "no existen citas disponibles",
    "actualmente no hay citas",
)
SUCCESS = (
    "ha sido reservada",
    "ha sido concertada",
    "se ha reservado correctamente",
    "cita confirmada",
)
DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


@dataclass
class Result:
    kind: str
    message: str = ""
    image: bytes | None = None
    page_image: bytes | None = None
    field: str | None = None


class IcpSession:
    def __init__(self, headless: bool, visible: bool = False) -> None:
        self.headless = headless
        self.visible = visible
        self.cancel = False
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.dialog_message = ""
        self.prev_fp = ""
        self.stuck = 0
        self.note = ""
        self.chosen_office = ""
        self.chosen_slot = ""
        self._cert_url = ""

    async def open(self) -> None:
        # A hidden Chrome window is blocked by the network firewall before the
        # province list exists. Real Chrome, kept off-screen, gets the form.
        try:
            await self._launch(offscreen=True)
            if self.visible:
                await self.show_window()
            if await self._load_start():
                log.info("opened %s", self.page.url)
                return
            if await self._rate_limited():
                last_error = "Сайт ответил: слишком много запросов."
            elif await self._blocked():
                last_error = "Сеть закрыла доступ к icpplus. Повтори чуть позже."
            else:
                last_error = "Сайт не показал список провинций."
        except Exception as exc:
            last_error = str(exc).splitlines()[0]
        await self.close()
        raise RuntimeError(last_error)

    async def _launch(self, offscreen: bool) -> None:
        self.playwright = await async_playwright().start()
        hidden = self.headless and not offscreen
        args = ["--disable-blink-features=AutomationControlled"]
        if offscreen:
            args.extend(
                [
                    "--window-position=-32000,-32000",
                    "--window-size=1280,900",
                    "--start-minimized",
                ]
            )
        self.browser = await self.playwright.chromium.launch(
            channel="chrome",
            headless=hidden,
            args=args,
            slow_mo=700 if self.visible else 0,
        )
        self.context = await self.browser.new_context(
            locale="es-ES",
            timezone_id="Europe/Madrid",
            viewport={"width": 1280, "height": 900},
        )
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self.page = await self.context.new_page()
        self.page.set_default_timeout(25000)
        self.page.on("dialog", self._on_dialog)

    async def _load_start(self) -> bool:
        await self.page.goto(START_URL, wait_until="domcontentloaded", timeout=45000)
        if await self._rate_limited() or await self._blocked():
            return False
        try:
            # The province list is drawn by the page a couple of seconds after load.
            await self.page.wait_for_selector("select#form, select[name='form']", timeout=25000)
        except Exception:
            return False
        return not await self._blocked()

    async def close(self) -> None:
        for item in (self.context, self.browser):
            try:
                if item is not None:
                    await item.close()
            except Exception:
                pass
        try:
            if self.playwright is not None:
                await self.playwright.stop()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    async def _on_dialog(self, dialog: Dialog) -> None:
        self.dialog_message = dialog.message or ""
        await dialog.accept()

    async def peek_province(self, province: str) -> tuple[list[str], list[str], str]:
        """Return office labels, huellas trámites, and a status: ok, waiting, or missing."""
        await self._choose_province(province)
        try:
            await self.page.wait_for_selector("#sede", timeout=15000)
        except Exception:
            if await self._no_citas():
                return [], [], "waiting"
            return [], [], "missing"
        offices = [
            item["label"]
            for item in await self._options("#sede")
            if not is_placeholder(item["label"], item["value"])
        ]
        pick = next((item for item in offices if "cualquier" in norm(item)), None)
        if pick is None and offices:
            pick = offices[0]
        if pick is not None:
            options = await self._options("#sede")
            match = next(item for item in options if item["label"] == pick)
            await self.page.select_option("#sede", value=match["value"])
            try:
                await self.page.wait_for_function(
                    """() => [...document.querySelectorAll("select[id^='tramiteGrupo']")]
                        .some(s => [...s.options].some(o => /toma de huellas/i.test(o.textContent || '')))""",
                    timeout=8000,
                )
            except Exception:
                pass
        tramites: list[str] = []
        for selector in await self._tramite_selectors():
            tramites.extend(
                item["label"]
                for item in await self._options(selector)
                if is_huellas(item["label"])
            )
        seen: set[str] = set()
        unique = [label for label in tramites if not (label in seen or seen.add(label))]
        if not unique:
            return offices, [], "missing"
        return offices, unique, "ok"

    async def drive(self, data: dict) -> Result:
        for _ in range(16):
            if self.cancel:
                return Result("cancelled", "Остановила.")
            result = await self._step(data)
            if result is not None:
                return result
        return Result("error", "Форма не дошла до конца. Напиши /status и запусти ещё раз.")

    async def submit_captcha(self, data: dict, text: str) -> Result:
        field = await self._captcha_input()
        if field is None:
            return Result("error", "Поле капчи уже исчезло. Ищу cita заново.")
        await field.fill(text.strip())
        await self._click_forward()
        return await self.drive(data)

    async def _step(self, data: dict) -> Result | None:
        await self._remember_note()
        fingerprint = await self._fingerprint()
        if fingerprint == self.prev_fp:
            self.stuck += 1
            if self.stuck >= 2:
                detail = await self._error_text() or await self._excerpt()
                return Result("error", "Страница не меняется.\n" + detail)
        else:
            self.stuck = 0
        self.prev_fp = fingerprint
        log.info("page %s", page_name(self.page.url))
        if self.visible:
            await self.page.wait_for_timeout(1600)

        if self.dialog_message:
            message = self.dialog_message
            self.dialog_message = ""
            mapped = self._from_site_message(message)
            if mapped is not None:
                if mapped.kind == "captcha_rejected":
                    return await self._captcha_result(mapped.message)
                return mapped

        if await self._rate_limited():
            return Result("error", "Сайт ответил: слишком много запросов.")
        if await self._rejected():
            return Result(
                "error",
                "Сайт отклонил запрос. Подожду и открою обычный Chrome ещё раз.",
            )
        if await self._success():
            return await self._booked()
        if await self._captcha_present():
            return await self._captcha_result()

        site_error = await self._error_text()
        if site_error:
            mapped = self._from_site_message(site_error)
            if mapped is not None:
                if mapped.kind == "captcha_rejected":
                    return await self._captcha_result(mapped.message)
                return mapped

        if await self._no_citas():
            return Result("no_citas", "Свободных citas сейчас нет.")
        if await self._visible("select#form, select[name='form']"):
            await self._choose_province(str(data["province"]))
            return None
        if await self._visible("#sede") and not await self._sede_ok(data):
            problem = await self._choose_sede(data)
            return problem
        if await self._needs_tramite(data):
            problem = await self._choose_tramite(data)
            return problem
        if await self._visible("#txtIdCitado"):
            problem = await self._fill_personal(data)
            if problem is not None:
                return problem
            await self._click_forward()
            return None
        if await self._has_certificate_choice():
            await self.show_window()
            self._cert_url = self.page.url
            kind = await self._choose_certificate()
            if kind == "radio":
                await self._click_forward()
            return Result(
                "need_cert",
                "На компьютере откроется окно Chrome. Выбери сертификат — это ключ с этого ПК. "
                "Когда выберешь, я продолжу сама. Телефон для этого шага не нужен.",
            )
        if await self._visible("#btnEntrar"):
            await self._click("#btnEntrar")
            return None
        if await self._visible("#idSede"):
            missed = await self._choose_slot_office(data)
            if missed is not None:
                return missed
            await self._click_forward()
            return None
        if await self._pick_earliest_slot():
            await self._click_forward()
            return None
        if await self._visible("#txtTelefonoCitado, #emailUNO, input[name='txtTelefonoCitado']"):
            await self._fill_contact(data)
            await self._click_forward()
            return None
        if await self._click_forward():
            return None
        return Result("error", "Не поняла эту страницу.\n" + await self._excerpt())

    def _from_site_message(self, message: str) -> Result | None:
        folded = norm(message)
        if not folded:
            return None
        if any(phrase in folded for phrase in NO_CITAS):
            return Result("no_citas", message.strip())
        if any(phrase in folded for phrase in ("captcha", "caracteres de la imagen", "codigo de verificacion")):
            return Result("captcha_rejected", message.strip())
        if len(folded) > 300:
            return None
        looks_wrong = any(word in folded for word in ("incorrect", "obligator", "debe", "no es", "error", "valido"))
        field = map_field_error(folded)
        if field and looks_wrong:
            return Result("bad_field", message.strip(), field=field)
        if looks_wrong:
            return Result("error", message.strip())
        return None

    async def _choose_province(self, province: str) -> None:
        selector = "select#form"
        if not await self._visible(selector):
            selector = "select[name='form']"
        options = await self._options(selector)
        match = match_label(province, options, aliases=_province_aliases())
        if match is None:
            raise RuntimeError(f"Провинции «{province}» нет в списке сайта.")
        value = match["value"]
        if value.startswith("http") or value.startswith("/"):
            url = value if value.startswith("http") else "https://icp.administracionelectronica.gob.es" + value
            await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await self.page.wait_for_timeout(600)
            return
        await self.page.select_option(selector, value=value)
        await self._click("#btnAceptar")

    async def _choose_sede(self, data: dict) -> Result | None:
        options = await self._options("#sede")
        real = [item for item in options if not is_placeholder(item["label"], item["value"])]
        if data.get("office") == "cualquier":
            match = next((item for item in real if "cualquier" in norm(item["label"])), None)
            if match is None:
                return Result(
                    "bad_field",
                    "Пункта «любая комиссария» нет. Пришли номер:\n" + _numbered(real),
                    field="office",
                )
        else:
            match = match_label(str(data.get("office", "")), real)
            if match is None:
                return Result(
                    "bad_field",
                    "Не нашла такую комиссарию. Пришли номер:\n" + _numbered(real),
                    field="office",
                )
        await self.page.select_option("#sede", value=match["value"])
        self.chosen_office = match["label"]
        try:
            await self.page.wait_for_function(
                """() => [...document.querySelectorAll("select[id^='tramiteGrupo']")]
                    .some(s => s.options.length > 1)""",
                timeout=8000,
            )
        except Exception:
            pass
        return None

    async def _sede_ok(self, data: dict) -> bool:
        label = await self._selected_label("#sede")
        if not label or is_placeholder(label, "x"):
            return False
        if data.get("office") == "cualquier":
            return "cualquier" in norm(label)
        return match_label(str(data.get("office", "")), [{"label": label, "value": "1"}]) is not None

    async def _needs_tramite(self, data: dict) -> bool:
        for selector in await self._tramite_selectors():
            selected = await self._selected_label(selector)
            if data.get("tramite") and has_phrase(norm(selected), norm(str(data["tramite"]))):
                return False
            if not data.get("tramite") and is_huellas(selected):
                return False
            options = await self._options(selector)
            if any(is_huellas(item["label"]) for item in options):
                return True
        return False

    async def _choose_tramite(self, data: dict) -> Result | None:
        for selector in await self._tramite_selectors():
            options = await self._options(selector)
            huellas = [item for item in options if is_huellas(item["label"])]
            if not huellas:
                continue
            if data.get("tramite"):
                match = match_label(str(data["tramite"]), huellas)
                if match is None:
                    return Result(
                        "bad_field",
                        "Не нашла этот trámite. Пришли номер:\n" + _numbered(huellas),
                        field="tramite",
                    )
            elif len(huellas) == 1:
                match = huellas[0]
            else:
                return Result(
                    "bad_field",
                    "На сайте несколько trámites про huellas. Пришли номер:\n" + _numbered(huellas),
                    field="tramite",
                )
            await self.page.select_option(selector, value=match["value"])
            return None
        return Result("error", "В этой провинции нет trámite toma de huellas.")

    async def _fill_personal(self, data: dict) -> Result | None:
        await self._choose_doc_type(str(data.get("doc_type", "nie")))
        await self.page.fill("#txtIdCitado", str(data["doc_number"]))
        if await self._visible("#txtDesCitado"):
            await self.page.fill("#txtDesCitado", str(data["full_name"]))
        for selector in ("#txtPaisNac", "#txtPaisNacionalidad"):
            if not await self._visible(selector):
                continue
            options = await self._options(selector)
            match = match_label(str(data["nationality"]), options, aliases=_country_aliases())
            if match is None:
                sample = ", ".join(item["label"] for item in options if not is_placeholder(item["label"], item["value"]))
                return Result(
                    "bad_field",
                    "Этой страны нет в списке сайта. Напиши её по-испански.\n" + sample[:1500],
                    field="nationality",
                )
            await self.page.select_option(selector, value=match["value"])
        if await self._visible("#txtAnnoCitado"):
            await self.page.fill("#txtAnnoCitado", str(data["birth_year"]))
        return None

    async def _choose_doc_type(self, doc_type: str) -> None:
        ids = {
            "nie": ("rdbTipoDocNie",),
            "pasaporte": ("rdbTipoDocPas", "rdbTipoDocPasDdi"),
            "dni": ("rdbTipoDocDni",),
        }.get(doc_type, ())
        for element_id in ids:
            loc = self.page.locator(f"#{element_id}")
            if await loc.count() and await loc.is_visible():
                try:
                    await loc.check(force=True)
                except Exception:
                    await loc.click(force=True)
                await self.page.wait_for_timeout(200)
                return

    async def _choose_slot_office(self, data: dict) -> Result | None:
        options = [
            item
            for item in await self._options("#idSede")
            if not is_placeholder(item["label"], item["value"])
        ]
        if not options:
            return None
        if data.get("office") != "cualquier":
            match = match_label(str(data.get("office", "")), options)
            if match is None:
                return Result("no_citas", "В выбранной комиссарии мест нет.")
        else:
            dated = [(slot_sort_key(item["label"]), item) for item in options]
            dated = [item for item in dated if item[0][0] < 9000]
            match = sorted(dated)[0][1] if dated else options[0]
        await self.page.select_option("#idSede", value=match["value"])
        self.chosen_office = match["label"]
        return None

    async def _pick_earliest_slot(self) -> bool:
        radios = await self.page.evaluate(
            """() => [...document.querySelectorAll('input[type=radio]')].filter(el => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden';
            }).map(el => {
                let label = '';
                if (el.id) {
                    const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (lab) label = lab.innerText;
                }
                if (!label && el.parentElement) label = el.parentElement.innerText;
                return {id: el.id || '', name: el.name || '', value: el.value || '', label: (label || '').trim()};
            })"""
        )
        candidates = []
        for radio in radios:
            name = norm(radio["name"] + " " + radio["label"])
            if "tipodoc" in name or "rdbtipodoc" in name:
                continue
            if DATE_RE.search(radio["label"]) or "cita" in name or "hora" in name:
                candidates.append(radio)
        if not candidates:
            return False
        best = sorted(candidates, key=lambda item: slot_sort_key(item["label"]))[0]
        if best["id"]:
            locator = self.page.locator(f"[id='{best['id']}']")
        else:
            locator = self.page.locator(f"input[type=radio][value='{best['value']}']").first
        try:
            await locator.check(force=True)
        except Exception:
            await locator.click(force=True)
        self.chosen_slot = " ".join(best["label"].split())
        return True

    async def _fill_contact(self, data: dict) -> None:
        phone = self.page.locator("#txtTelefonoCitado, input[name='txtTelefonoCitado']").first
        if await phone.count() and await phone.is_visible():
            await phone.fill(str(data["phone"]))
        emails = self.page.locator(
            "#emailUNO, #emailDOS, #emailUno, #emailDos, input[type='email'], input[id*='email' i], input[name*='email' i]"
        )
        for index in range(await emails.count()):
            field = emails.nth(index)
            if await field.is_visible():
                await field.fill(str(data["email"]))

    async def show_window(self) -> None:
        try:
            client = await self.page.context.new_cdp_session(self.page)
            info = await client.send("Browser.getWindowForTarget")
            await client.send(
                "Browser.setWindowBounds",
                {
                    "windowId": info["windowId"],
                    "bounds": {
                        "left": 60,
                        "top": 40,
                        "width": 1200,
                        "height": 900,
                        "windowState": "normal",
                    },
                },
            )
        except Exception:
            log.info("could not bring Chrome to the front")

    async def _has_certificate_choice(self) -> bool:
        if await self._visible("#txtIdCitado"):
            return False
        return await self._choose_certificate(click=False) != ""

    async def _choose_certificate(self, click: bool = True) -> str:
        if await self._visible("#txtIdCitado"):
            return ""
        kind = await self.page.evaluate(
            """click => {
                const textOf = el => ((el.innerText || el.value || el.labels?.[0]?.innerText || '') + '')
                    .toLowerCase().replace('@', 'a');
                const visible = el => {
                    const style = getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden';
                };
                const rank = text => {
                    if (text.includes('sin') && text.includes('clave')) return 0;
                    if (text.includes('dnie') || text.includes('dni electron')) return 3;
                    if (text.includes('certificado') && (text.includes('con') || text.includes('acceso') || text.includes('presenta') || text.includes('electron'))) return 3;
                    if (text.includes('clave') && text.includes('con')) return 2;
                    return 0;
                };
                const radios = [...document.querySelectorAll('input[type=radio]')].filter(visible);
                const radio = radios.find(el => rank(textOf(el)) >= 2);
                if (radio) {
                    if (click) radio.click();
                    return 'radio';
                }
                const nodes = [...document.querySelectorAll('a, button, input[type=button], input[type=submit]')].filter(visible);
                const button = nodes.find(el => rank(textOf(el)) >= 2);
                if (button) {
                    if (click) button.click();
                    return 'button';
                }
                return '';
            }""",
            click,
        )
        if click and kind:
            await self._pause()
        return kind or ""

    async def wait_past_certificate(self, seconds: int = 180) -> bool:
        before = self._cert_url or self.page.url
        for _ in range(seconds):
            if self.cancel:
                return False
            if self.page.url != before or await self._visible("#txtIdCitado"):
                await self.page.wait_for_timeout(1000)
                return True
            await self.page.wait_for_timeout(1000)
        return False

    async def _click_forward(self) -> bool:
        for selector in FORWARD:
            if await self._visible(selector):
                return await self._click(selector)
        for name in ("Aceptar", "Entrar", "Enviar", "Siguiente", "Confirmar"):
            locator = self.page.locator(
                f"input[type=button][value='{name}'], input[type=submit][value='{name}']"
            ).first
            if await locator.count() and await locator.is_visible():
                return await self._click_locator(locator)
        return False

    async def _click(self, selector: str) -> bool:
        locator = self.page.locator(selector).first
        if not await locator.count() or not await locator.is_visible():
            return False
        return await self._click_locator(locator)

    async def _click_locator(self, locator) -> bool:
        try:
            await locator.click(timeout=5000)
        except Exception:
            return False
        await self._pause()
        return True

    async def _pause(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await self.page.wait_for_timeout(1600 if self.visible else 400)

    async def _captcha_present(self) -> bool:
        if await self._captcha_image_locator() is not None:
            return True
        return await self._captcha_input() is not None

    async def _captcha_input(self):
        selectors = (
            "input#captcha",
            "input[name='captcha']",
            "input[id*='aptcha' i]",
            "input[name*='aptcha' i]",
            "input#txtCaptcha",
            "input[name='txtCaptcha']",
        )
        for selector in selectors:
            locator = self.page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                return locator
        return None

    async def _captcha_image_locator(self):
        selectors = (
            "img#captchaImg",
            "img#captchaImage",
            "img[id*='aptcha' i]",
            "img[src*='aptcha' i]",
            "img[alt*='aptcha' i]",
            "img[src*='simpleCaptcha' i]",
        )
        for selector in selectors:
            locator = self.page.locator(selector).first
            if not await locator.count() or not await locator.is_visible():
                continue
            box = await locator.bounding_box()
            if box and box["width"] > 40 and box["height"] > 20:
                return locator
        return None

    async def _captcha_result(self, prefix: str = "") -> Result:
        image = None
        locator = await self._captcha_image_locator()
        if locator is not None:
            try:
                image = await locator.screenshot()
            except Exception:
                image = None
        page_image = None
        try:
            page_image = await self.page.screenshot()
        except Exception:
            page_image = None
        lines = [line for line in (prefix, self.chosen_office, self.chosen_slot, self.note) if line]
        return Result("captcha", "\n".join(lines), image=image, page_image=page_image)

    async def _booked(self) -> Result:
        text = await self.page.inner_text("body")
        kept = []
        for line in text.splitlines():
            folded = norm(line)
            if any(phrase in folded for phrase in SUCCESS) or "justificante" in folded or DATE_RE.search(line):
                cleaned = " ".join(line.split())
                if cleaned:
                    kept.append(cleaned)
        message = "\n".join(kept[:25]) or "Cita подтверждена. Посмотри номер в окне Chrome."
        image = None
        try:
            image = await self.page.screenshot(full_page=True)
        except Exception:
            image = None
        return Result("booked", message[:3500], page_image=image)

    async def _success(self) -> bool:
        text = norm(await self.page.inner_text("body"))
        return any(phrase in text for phrase in SUCCESS)

    async def _no_citas(self) -> bool:
        if await self._visible("#txtIdCitado") or await self._visible("select#form"):
            return False
        text = norm(await self.page.inner_text("body"))
        return any(phrase in text for phrase in NO_CITAS)

    async def _rate_limited(self) -> bool:
        try:
            text = (await self.page.inner_text("body")).lower()
        except Exception:
            return False
        return "too many requests" in text or "demasiadas solicitudes" in text

    async def _blocked(self) -> bool:
        try:
            text = (await self.page.inner_text("body")).lower()
        except Exception:
            return False
        return any(
            phrase in text
            for phrase in (
                "request rejected",
                "requested url was rejected",
                "intrusion prevention",
                "fortigate",
            )
        )

    async def _rejected(self) -> bool:
        return await self._blocked()

    async def _visible(self, selector: str) -> bool:
        locator = self.page.locator(selector).first
        try:
            return await locator.count() > 0 and await locator.is_visible()
        except Exception:
            return False

    async def _options(self, selector: str) -> list[dict]:
        return await self.page.eval_on_selector(
            selector,
            """el => [...el.options].map(o => ({
                value: o.value || '',
                label: (o.textContent || '').replace(/\\s+/g, ' ').trim()
            }))""",
        )

    async def _selected_label(self, selector: str) -> str:
        return await self.page.eval_on_selector(
            selector,
            """el => {
                const option = el.selectedOptions[0];
                return option ? (option.textContent || '').replace(/\\s+/g, ' ').trim() : '';
            }""",
        )

    async def _tramite_selectors(self) -> list[str]:
        ids = await self.page.eval_on_selector_all(
            "select[id^='tramiteGrupo'], select[name^='tramiteGrupo']",
            "els => els.map(el => el.id || el.name)",
        )
        return [f'[id="{item}"]' for item in ids if item]

    async def _error_text(self) -> str:
        return await self.page.evaluate(
            """() => {
                const parts = [];
                for (const el of document.querySelectorAll('.mf-msg__error, .mensajeError, #divError')) {
                    if (el.offsetParent === null) continue;
                    const text = (el.innerText || '').trim();
                    if (text) parts.push(text);
                }
                return parts.join('\\n').slice(0, 800);
            }"""
        )

    async def _excerpt(self) -> str:
        text = await self.page.inner_text("body")
        lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        return "\n".join(lines[:12])[:800]

    async def _remember_note(self) -> None:
        text = await self.page.inner_text("body")
        lines = []
        for line in text.splitlines():
            folded = norm(line)
            if DATE_RE.search(line) or any(word in folded for word in ("hora", "oficina", "direccion", "comisaria")):
                cleaned = " ".join(line.split())
                if cleaned and cleaned not in lines:
                    lines.append(cleaned)
        if lines:
            self.note = "\n".join(lines[:12])[:1000]

    async def _fingerprint(self) -> str:
        try:
            return await self.page.evaluate(
                """() => {
                    const selects = [...document.querySelectorAll('select')].map(s => s.id + ':' + s.value).join(',');
                    const inputs = [...document.querySelectorAll('input')].filter(i => i.type !== 'hidden')
                        .map(i => (i.id || i.name) + ':' + (i.value ? '1' : '0') + (i.checked ? 'c' : '')).join(',');
                    return location.pathname + '|' + selects + '|' + inputs + '|' + document.body.innerText.length;
                }"""
            )
        except Exception:
            return self.page.url if self.page else ""


def page_name(url: str) -> str:
    name = urlsplit(url).path.rstrip("/").split("/")[-1]
    return name.split(";")[0] or "index"


def slot_sort_key(label: str) -> tuple[int, int, int, int, int]:
    found = DATE_RE.search(label)
    if not found:
        return (9999, 12, 31, 23, 59)
    day, month, year = (int(part) for part in found.groups())
    clock = TIME_RE.search(label)
    hour, minute = (int(clock.group(1)), int(clock.group(2))) if clock else (0, 0)
    return (year, month, day, hour, minute)


def map_field_error(folded: str) -> str | None:
    if any(word in folded for word in ("nie", "documento", "pasaporte")):
        return "doc_number"
    if "nombre" in folded or "apellido" in folded:
        return "full_name"
    if "telefono" in folded:
        return "phone"
    if any(word in folded for word in ("correo", "email", "e-mail")):
        return "email"
    if "nacionalidad" in folded or re.search(r"(?<![a-z])pais(?![a-z])", folded):
        return "nationality"
    if "oficina" in folded or "comisaria" in folded:
        return "office"
    return None


def _numbered(options: list[dict]) -> str:
    return "\n".join(f"{index}. {item['label']}" for index, item in enumerate(options, start=1))[:3500]


def _province_aliases() -> dict[str, str]:
    from fields import PROVINCE_ALIASES

    return PROVINCE_ALIASES


def _country_aliases() -> dict[str, str]:
    from fields import COUNTRY_ALIASES

    return COUNTRY_ALIASES
