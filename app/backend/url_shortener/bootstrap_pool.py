from url_shortener.api.dependencies import get_pool_service
from url_shortener.storage.database import get_database


def main() -> None:
    get_database().create_all()
    status = get_pool_service().bootstrap_target_pool()
    print(status)


if __name__ == "__main__":
    main()
