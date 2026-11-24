#include "OrchestratorUtils.h"

#include "Dom/JsonObject.h"
#include "Misc/Guid.h"
#include "Policies/CondensedJsonPrintPolicy.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

namespace OrchestratorUtils
{
	FString ToJson(const TSharedRef<FJsonObject>& Obj)
	{
		FString Out;
		TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer =
			TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Out);
		FJsonSerializer::Serialize(Obj, Writer);
		return Out;
	}

	TSharedPtr<FJsonObject> ParseJson(const FString& Text)
	{
		TSharedPtr<FJsonObject> Obj;
		TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Text);
		if (!FJsonSerializer::Deserialize(Reader, Obj))
		{
			return nullptr;
		}
		return Obj;
	}

	bool CanImport(const FString& Format)
	{
		static const TSet<FString> Formats = {
			TEXT("fbx"), TEXT("glb"), TEXT("gltf"), TEXT("obj"), TEXT("abc"),
			TEXT("png"), TEXT("jpg"), TEXT("jpeg"), TEXT("tga"), TEXT("bmp"), TEXT("exr"), TEXT("hdr"),
			TEXT("tif"), TEXT("tiff"), TEXT("psd"), TEXT("webp"),
			TEXT("wav"), TEXT("ogg"), TEXT("flac"),
		};
		return Formats.Contains(Format.ToLower());
	}

	FString SafeName(const FString& In, const FString& Fallback)
	{
		FString Out;
		bool bLastUnderscore = false;
		for (TCHAR C : In.TrimStartAndEnd())
		{
			const bool bOk = C < 128 && (FChar::IsAlnum(C) || C == TEXT('_') || C == TEXT('-'));
			if (bOk)
			{
				Out.AppendChar(C);
				bLastUnderscore = false;
			}
			else if (!bLastUnderscore)
			{
				Out.AppendChar(TEXT('_'));
				bLastUnderscore = true;
			}
		}
		Out = Out.Replace(TEXT("-"), TEXT("_"));
		FString Trimmed = Out;
		while (Trimmed.StartsWith(TEXT("_")))
		{
			Trimmed.RightChopInline(1);
		}
		while (Trimmed.EndsWith(TEXT("_")))
		{
			Trimmed.LeftChopInline(1);
		}
		return Trimmed.IsEmpty() ? Fallback : Trimmed.Left(64);
	}

	static void Append(TArray<uint8>& Body, const FString& Text)
	{
		FTCHARToUTF8 Utf8(*Text);
		Body.Append(reinterpret_cast<const uint8*>(Utf8.Get()), Utf8.Length());
	}

	FString BuildMultipartFiles(const TMap<FString, FString>& Fields, const TArray<FMultipartFile>& Files, TArray<uint8>& OutBody)
	{
		const FString Boundary = TEXT("orch") + FGuid::NewGuid().ToString(EGuidFormats::Digits);
		OutBody.Reset();
		for (const TPair<FString, FString>& Field : Fields)
		{
			Append(OutBody, FString::Printf(TEXT("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"),
				*Boundary, *Field.Key, *Field.Value));
		}
		for (const FMultipartFile& File : Files)
		{
			Append(OutBody, FString::Printf(TEXT("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"),
				*Boundary, *File.Field, *File.FileName, *File.Mime));
			OutBody.Append(File.Data);
			Append(OutBody, TEXT("\r\n"));
		}
		Append(OutBody, FString::Printf(TEXT("--%s--\r\n"), *Boundary));
		return TEXT("multipart/form-data; boundary=") + Boundary;
	}

	FString BuildMultipart(const TMap<FString, FString>& Fields, const FString& FileName, const FString& Mime,
		const TArray<uint8>& FileData, TArray<uint8>& OutBody)
	{
		TArray<FMultipartFile> Files;
		Files.Add({TEXT("file"), FileName, Mime, FileData});
		return BuildMultipartFiles(Fields, Files, OutBody);
	}
}
