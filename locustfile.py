"""Load test for RateGuard's rate-limiting middleware.

Run against a live instance:

    uvicorn app.main:app &
    locust -f locustfile.py --host http://localhost:8000

Or headless, e.g. 20 users for 30 seconds:

    locust -f locustfile.py --host http://localhost:8000 \\
        --headless -u 20 -r 5 -t 30s
"""

from locust import HttpUser, task, between


class RateGuardUser(HttpUser):
    wait_time = between(0.05, 0.2)

    @task(5)
    def get_data(self):
        # Fixed key so repeated requests share one token bucket, the same
        # way one real API client would be throttled.
        with self.client.get(
            "/data",
            headers={"x-api-key": "locust-load-test"},
            catch_response=True,
        ) as response:
            # 429 is the middleware working as designed under load, not a
            # failed request -- only count other statuses as failures.
            if response.status_code in (200, 429):
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")

    @task(1)
    def get_health(self):
        # Same reasoning as /data: a 429 here means the limiter correctly
        # throttled this client, not that the request failed.
        with self.client.get("/health", catch_response=True) as response:
            if response.status_code in (200, 429):
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")
