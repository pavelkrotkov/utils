"""Typed Security.framework entry points used by the Keychain adapter."""

import ctypes as C


def security_framework():
    security = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer, length, status = C.c_void_p, C.c_uint32, C.c_int32
    security.SecKeychainSetUserInteractionAllowed.argtypes = [C.c_bool]
    security.SecKeychainFindGenericPassword.argtypes = [
        pointer,
        length,
        C.c_char_p,
        length,
        C.c_char_p,
        C.POINTER(length),
        C.POINTER(pointer),
        C.POINTER(pointer),
    ]
    security.SecKeychainFindGenericPassword.restype = status
    security.SecKeychainAddGenericPassword.argtypes = [
        pointer,
        length,
        C.c_char_p,
        length,
        C.c_char_p,
        length,
        C.c_char_p,
        C.POINTER(pointer),
    ]
    security.SecKeychainAddGenericPassword.restype = status
    security.SecKeychainItemModifyAttributesAndData.argtypes = [
        pointer,
        pointer,
        length,
        C.c_char_p,
    ]
    security.SecKeychainItemModifyAttributesAndData.restype = status
    security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
    core.CFRelease.argtypes = [pointer]
    return security, core
