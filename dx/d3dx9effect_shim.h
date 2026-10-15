#pragma once

#include "com.h"

struct X86;

void X_D3DXCreateEffectFromFileA(X86 *c);
void X_D3DX33CreateEffectPool(X86 *c);
void efx_create(X86 *c, ComObj *dev, uint32_t pool, uint32_t flags, uint32_t out,
                const uint8_t *bytes, size_t size);

void Efx_GetDesc(X86 *c);
void Efx_GetParameterDesc(X86 *c);
void Efx_GetTechniqueDesc(X86 *c);
void Efx_GetPassDesc(X86 *c);
void Efx_GetParameter(X86 *c);
void Efx_GetParameterByName(X86 *c);
void Efx_GetParameterBySemantic(X86 *c);
void Efx_GetParameterElement(X86 *c);
void Efx_GetTechnique(X86 *c);
void Efx_GetTechniqueByName(X86 *c);
void Efx_GetPass(X86 *c);
void Efx_GetPassByName(X86 *c);
void Efx_GetAnnotation(X86 *c);
void Efx_GetAnnotationByName(X86 *c);
void Efx_SetValue(X86 *c);
void Efx_SetRawValue(X86 *c);
void Efx_GetValue(X86 *c);
void Efx_SetBool(X86 *c);
void Efx_GetBool(X86 *c);
void Efx_SetBoolArray(X86 *c);
void Efx_GetBoolArray(X86 *c);
void Efx_SetInt(X86 *c);
void Efx_GetInt(X86 *c);
void Efx_SetIntArray(X86 *c);
void Efx_GetIntArray(X86 *c);
void Efx_SetFloat(X86 *c);
void Efx_GetFloat(X86 *c);
void Efx_SetFloatArray(X86 *c);
void Efx_GetFloatArray(X86 *c);
void Efx_SetVector(X86 *c);
void Efx_GetVector(X86 *c);
void Efx_SetVectorArray(X86 *c);
void Efx_GetVectorArray(X86 *c);
void Efx_SetMatrix(X86 *c);
void Efx_GetMatrix(X86 *c);
void Efx_SetMatrixArray(X86 *c);
void Efx_GetMatrixArray(X86 *c);
void Efx_SetMatrixPointerArray(X86 *c);
void Efx_SetMatrixTranspose(X86 *c);
void Efx_GetMatrixTranspose(X86 *c);
void Efx_SetMatrixTransposeArray(X86 *c);
void Efx_SetMatrixTransposePointerArray(X86 *c);
void Efx_GetMatrixTransposeArray(X86 *c);
void Efx_GetString(X86 *c);
void Efx_SetTexture(X86 *c);
void Efx_GetTexture(X86 *c);
void Efx_GetPool(X86 *c);
void Efx_SetTechnique(X86 *c);
void Efx_GetCurrentTechnique(X86 *c);
void Efx_ValidateTechnique(X86 *c);
void Efx_FindNextValidTechnique(X86 *c);
void Efx_IsParameterUsed(X86 *c);
void Efx_Begin(X86 *c);
void Efx_BeginPass(X86 *c);
void Efx_CommitChanges(X86 *c);
void Efx_EndPass(X86 *c);
void Efx_End(X86 *c);
void Efx_GetDevice(X86 *c);
void Efx_OnLostDevice(X86 *c);
void Efx_OnResetDevice(X86 *c);
void Efx_SetStateManager(X86 *c);
void Efx_GetStateManager(X86 *c);

void d3dx_effect_destroy(ComObj *o);
void d3dx_effect_pool_destroy(ComObj *o);
