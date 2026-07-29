from locust import HttpUser, between, task
import random
import string
import ssl

# ОТКЛЮЧАЕМ ПРОВЕРКУ SSL ДЛЯ САМОПОДПИСАННЫХ СЕРТИФИКАТОВ
ssl._create_default_https_context = ssl._create_unverified_context


class UrlShortenerUser(HttpUser):
    wait_time = between(0.5, 2)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Отключаем проверку сертификата для клиента
        self.client.verify = False

    def generate_random_url(self):
        """Генерирует случайный URL для тестирования"""
        domains = ["example.com", "test.org", "demo.net"]
        paths = ["products", "blog", "docs", "api"]
        domain = random.choice(domains)
        path = random.choice(paths)
        return f"https://{domain}/{path}/{random.randint(1, 10000)}"

    def on_start(self):
        """Создаем тестовую ссылку при старте каждого пользователя"""
        response = self.client.post(
            "/api/v1/links",
            json={"original_url": "https://example.com/load-test-start"},
            name="[Init] Create test link",
            verify=False  # Явно отключаем проверку
        )
        if response.status_code in [200, 201]:
            data = response.json()
            self.short_code = data.get("short_code") or data.get("code")
            print(f"✓ User initialized with short_code: {self.short_code}")
        else:
            print(f"✗ Init failed with status {response.status_code}")
            self.short_code = None

    @task(10)
    def redirect_to_link(self):
        """Переход по короткой ссылке - основной сценарий"""
        if self.short_code:
            with self.client.get(
                    f"/{self.short_code}",
                    name="GET /{short_code} - redirect",
                    allow_redirects=False,
                    verify=False,
                    catch_response=True
            ) as response:
                if response.status_code in [301, 302, 303, 307, 308]:
                    response.success()
                elif response.status_code == 404:
                    response.failure("Short code not found")
                elif response.status_code == 400:
                    response.failure(f"Bad request: {response.text[:100]}")
                else:
                    response.failure(f"Expected redirect, got {response.status_code}")

    @task(5)
    def create_short_link(self):
        """Создание новой короткой ссылки"""
        payload = {
            "original_url": self.generate_random_url()
        }

        # Иногда добавляем TTL или custom alias
        if random.random() < 0.2:
            payload["expires_in_hours"] = random.choice([1, 24, 168])
        if random.random() < 0.1:
            payload["custom_alias"] = f"test_{random.randint(1, 1000)}"

        with self.client.post(
                "/api/v1/links",
                json=payload,
                name="POST /api/v1/links - create",
                verify=False,
                catch_response=True
        ) as response:
            if response.status_code in [200, 201]:
                data = response.json()
                short_code = data.get("short_code") or data.get("code")
                if short_code:
                    response.success()
                    # Сохраняем для будущих редиректов
                    if hasattr(self, 'short_codes'):
                        self.short_codes.append(short_code)
                    else:
                        self.short_codes = [short_code]
                else:
                    response.failure("Response missing short_code")
            elif response.status_code == 400:
                response.failure(f"Bad request: {response.text[:100]}")
            elif response.status_code == 429:
                response.failure("Rate limited")

    @task(3)
    def list_links(self):
        """Получение списка ссылок пользователя"""
        self.client.get(
            "/api/v1/links",
            name="GET /api/v1/links - list",
            verify=False
        )

    @task(2)
    def test_nonexistent_redirect(self):
        """Проверка несуществующей ссылки - должна вернуть 404"""
        fake_code = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
        with self.client.get(
                f"/{fake_code}",
                name="GET /{short_code} - 404 test",
                allow_redirects=False,
                verify=False,
                catch_response=True
        ) as response:
            if response.status_code == 404:
                response.success()
            elif response.status_code == 400:
                response.failure(f"Expected 404, got 400: {response.text[:100]}")
            else:
                response.failure(f"Expected 404, got {response.status_code}")

    @task(2)
    def get_link_stats(self):
        """Получение статистики по ссылке"""
        short_codes = getattr(self, 'short_codes', [])
        if short_codes:
            short_code = random.choice(short_codes)
            self.client.get(
                f"/api/v1/links/{short_code}/stats",
                name="GET /api/v1/links/{short_code}/stats",
                verify=False
            )