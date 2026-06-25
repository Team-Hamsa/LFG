def test_public_exports_are_importable():
    from surfaces._client import (
        AuthError,
        BadRequest,
        Event,
        LFGServiceClient,
        NotFound,
        ServiceError,
        ServiceUnavailable,
    )

    assert LFGServiceClient.__name__ == "LFGServiceClient"
    assert issubclass(AuthError, ServiceError)
    assert {BadRequest, NotFound, ServiceUnavailable}  # referenced
    assert Event.__name__ == "Event"
