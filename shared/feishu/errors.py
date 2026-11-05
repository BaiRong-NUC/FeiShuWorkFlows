from typing import Protocol, TypeVar


class FeishuApiError(RuntimeError):
    """Raised when a Feishu OpenAPI call fails."""


ServiceT = TypeVar("ServiceT")
DataT = TypeVar("DataT", covariant=True)


class ApiResponse(Protocol[DataT]):
    @property
    def data(self) -> DataT | None: ...

    @property
    def code(self) -> int | None: ...

    @property
    def msg(self) -> str | None: ...

    def success(self) -> bool: ...
    def get_log_id(self) -> str | None: ...


def require_service(service: ServiceT | None) -> ServiceT:
    if service is None:
        raise FeishuApiError("Feishu SDK service is not initialized; build the client before use")
    return service


def required_data(response: ApiResponse[DataT], action: str) -> DataT:
    if not response.success():
        message = f"failed to {action}: code={response.code}, msg={response.msg}"
        log_id = response.get_log_id()
        if log_id:
            message += f", log_id={log_id}"
        raise FeishuApiError(message)
    if response.data is None:
        raise FeishuApiError(f"failed to {action}: Feishu returned no data")
    return response.data
