#pragma once

#include "CoreMinimal.h"

class FJsonObject;

namespace OrchestratorUtils
{
	FString ToJson(const TSharedRef<FJsonObject>& Obj);
	TSharedPtr<FJsonObject> ParseJson(const FString& Text);

	/** Formats Unreal's own importers take (FBX/glTF/OBJ/Alembic, images, audio). */
	bool CanImport(const FString& Format);

	/** An ASCII-only package/asset name segment; Fallback when nothing usable is left. */
	FString SafeName(const FString& In, const FString& Fallback);

	struct FMultipartFile
	{
		FString Field;
		FString FileName;
		FString Mime;
		TArray<uint8> Data;
	};

	/** multipart/form-data body with any number of files; returns the Content-Type header value. */
	FString BuildMultipartFiles(const TMap<FString, FString>& Fields, const TArray<FMultipartFile>& Files, TArray<uint8>& OutBody);

	/** multipart/form-data body; returns the Content-Type header value. */
	FString BuildMultipart(const TMap<FString, FString>& Fields, const FString& FileName, const FString& Mime,
		const TArray<uint8>& FileData, TArray<uint8>& OutBody);
}
