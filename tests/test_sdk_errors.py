from surfaces._client.errors import (
    AuthError,
    BadRequest,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    error_for,
)


def test_error_for_returns_none_for_success():
    assert error_for(200, {"ok": True}) is None
    assert error_for(204, None) is None


def test_error_for_maps_status_and_parses_body():
    err = error_for(400, {"error": "bad input", "code": "bad_request"})
    assert isinstance(err, BadRequest)
    assert err.message == "bad input"
    assert err.code == "bad_request"
    assert err.status == 400


def test_error_for_maps_401_and_404():
    assert isinstance(error_for(401, {"error": "nope", "code": "bad_session"}), AuthError)
    assert isinstance(error_for(404, {"error": "gone", "code": "not_found"}), NotFound)


def test_error_for_5xx_is_service_unavailable():
    err = error_for(503, None)
    assert isinstance(err, ServiceUnavailable)
    assert err.status == 503


def test_error_for_unmapped_4xx_is_base_service_error():
    err = error_for(418, {"error": "teapot"})
    assert type(err) is ServiceError
    assert err.status == 418


def test_subclasses_are_service_errors():
    assert issubclass(AuthError, ServiceError)
    assert issubclass(ServiceUnavailable, ServiceError)
