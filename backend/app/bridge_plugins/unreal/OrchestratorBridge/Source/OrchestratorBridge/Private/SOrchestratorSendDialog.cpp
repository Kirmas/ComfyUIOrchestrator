#include "SOrchestratorSendDialog.h"

#include "OrchestratorBridgeSettings.h"
#include "OrchestratorConnection.h"
#include "OrchestratorUtils.h"

#include "Animation/AnimSequence.h"
#include "AssetExportTask.h"
#include "Dom/JsonObject.h"
#include "Engine/SkeletalMesh.h"
#include "Engine/StaticMesh.h"
#include "Engine/Texture.h"
#include "Exporters/Exporter.h"
#include "Exporters/FbxExportOption.h"
#include "Options/GLTFExportOptions.h"
#include "Framework/Application/SlateApplication.h"
#include "HAL/FileManager.h"
#include "HAL/IConsoleManager.h"
#include "Materials/Material.h"
#include "Materials/MaterialInstance.h"
#include "Interfaces/IHttpResponse.h"
#include "Misc/EngineVersion.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Input/SCheckBox.h"
#include "Widgets/Input/STextComboBox.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/SBoxPanel.h"
#include "Widgets/SWindow.h"
#include "Widgets/Text/STextBlock.h"

#define LOCTEXT_NAMESPACE "OrchestratorBridge"

void SOrchestratorSendDialog::Open(const TArray<FAssetData>& InAssets, TSharedPtr<FOrchestratorConnection> InConnection)
{
	TSharedRef<SWindow> Win = SNew(SWindow)
		.Title(LOCTEXT("SendTitle", "Send to Orchestrator"))
		.SizingRule(ESizingRule::Autosized)
		.SupportsMaximize(false)
		.SupportsMinimize(false);
	Win->SetContent(SNew(SOrchestratorSendDialog).Assets(InAssets).Connection(InConnection).Window(Win));
	FSlateApplication::Get().AddWindow(Win);
}

void SOrchestratorSendDialog::Construct(const FArguments& InArgs)
{
	Assets = InArgs._Assets;
	Connection = InArgs._Connection;
	Window = InArgs._Window;
	bPlace = GetDefault<UOrchestratorBridgeSettings>()->bPlaceAsCellByDefault;

	FString Names;
	for (int32 i = 0; i < Assets.Num() && i < 6; ++i)
	{
		Names += (i ? TEXT(", ") : TEXT("")) + Assets[i].AssetName.ToString();
	}
	if (Assets.Num() > 6)
	{
		Names += FString::Printf(TEXT(" ... (+%d)"), Assets.Num() - 6);
	}

	ChildSlot
	[
		SNew(SBox).MinDesiredWidth(420.f).Padding(12.f)
		[
			SNew(SVerticalBox)
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 0, 0, 8)
			[
				SNew(STextBlock).Text(FText::FromString(Names)).AutoWrapText(true)
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
			[
				SNew(STextBlock).Text(LOCTEXT("Project", "Project"))
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
			[
				SAssignNew(ProjectCombo, STextComboBox)
				.OptionsSource(&ProjectOptions)
				.OnSelectionChanged(this, &SOrchestratorSendDialog::OnProjectChanged)
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 6, 0, 2)
			[
				SNew(STextBlock).Text(LOCTEXT("Graph", "Graph"))
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
			[
				SAssignNew(GraphCombo, STextComboBox).OptionsSource(&GraphOptions)
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 8)
			[
				SNew(SCheckBox)
				.IsChecked_Lambda([this]() { return bPlace ? ECheckBoxState::Checked : ECheckBoxState::Unchecked; })
				.OnCheckStateChanged_Lambda([this](ECheckBoxState S) { bPlace = S == ECheckBoxState::Checked; })
				[
					SNew(STextBlock).Text(LOCTEXT("Place", "Also place as a cell (new row in the graph)"))
				]
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 0, 0, 8)
			[
				SNew(SCheckBox)
				.IsChecked_Lambda([this]() { return bSourceGeometry ? ECheckBoxState::Checked : ECheckBoxState::Unchecked; })
				.OnCheckStateChanged_Lambda([this](ECheckBoxState S) { bSourceGeometry = S == ECheckBoxState::Checked; })
				.ToolTipText(LOCTEXT("SourceTip", "Static meshes only. On: the full source geometry (a Nanite mesh can be millions of triangles -- a heavy file). Off: the render mesh (for Nanite, its light fallback). Skeletal meshes and animations always go with their skin."))
				[
					SNew(STextBlock).Text(LOCTEXT("Source", "Full source geometry (not the Nanite fallback)"))
				]
			]
			+ SVerticalBox::Slot().AutoHeight().Padding(0, 4)
			[
				SNew(STextBlock).Text_Lambda([this]() { return FText::FromString(Status); }).AutoWrapText(true)
			]
			+ SVerticalBox::Slot().AutoHeight().HAlign(HAlign_Right).Padding(0, 8, 0, 0)
			[
				SNew(SHorizontalBox)
				+ SHorizontalBox::Slot().AutoWidth().Padding(4, 0)
				[
					SNew(SButton)
					.Text(LOCTEXT("Send", "Send"))
					.IsEnabled_Lambda([this]() { return !bBusy && Projects.Num() > 0; })
					.OnClicked(this, &SOrchestratorSendDialog::OnSend)
				]
				+ SHorizontalBox::Slot().AutoWidth().Padding(4, 0)
				[
					SNew(SButton)
					.Text(LOCTEXT("Close", "Close"))
					.OnClicked(this, &SOrchestratorSendDialog::OnCancel)
				]
			]
		]
	];

	LoadTargets();
}

void SOrchestratorSendDialog::SetStatus(const FString& Text)
{
	Status = Text;
}

void SOrchestratorSendDialog::LoadTargets()
{
	bBusy = true;
	SetStatus(TEXT("Loading projects..."));
	TWeakPtr<SOrchestratorSendDialog> Weak = StaticCastSharedRef<SOrchestratorSendDialog>(AsShared());
	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = Connection->NewRequest(TEXT("GET"), TEXT("/api/bridge/device/targets"));
	Req->OnProcessRequestComplete().BindLambda([Weak](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		TSharedPtr<SOrchestratorSendDialog> Self = Weak.Pin();
		if (!Self.IsValid())
		{
			return;
		}
		Self->bBusy = false;
		if (!bOk || !Resp.IsValid() || Resp->GetResponseCode() != 200)
		{
			const int32 Code = Resp.IsValid() ? Resp->GetResponseCode() : 0;
			Self->SetStatus(Code == 401
				? FString(TEXT("This editor isn't paired (or its key expired): Tools > Orchestrator Bridge > Connect / Pair."))
				: FString::Printf(TEXT("Couldn't reach the orchestrator (%d). Check Project Settings > Plugins > Orchestrator Bridge."), Code));
			return;
		}
		TArray<TSharedPtr<FJsonValue>> List;
		TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Resp->GetContentAsString());
		if (!FJsonSerializer::Deserialize(Reader, List))
		{
			Self->SetStatus(TEXT("Unexpected answer from the orchestrator"));
			return;
		}
		Self->Projects.Reset();
		Self->ProjectOptions.Reset();
		for (const TSharedPtr<FJsonValue>& V : List)
		{
			const TSharedPtr<FJsonObject> P = V->AsObject();
			if (!P.IsValid())
			{
				continue;
			}
			FProject Project;
			Project.Id = P->GetStringField(TEXT("id"));
			Project.Name = P->GetStringField(TEXT("name"));
			const TArray<TSharedPtr<FJsonValue>>* Graphs = nullptr;
			if (P->TryGetArrayField(TEXT("graphs"), Graphs))
			{
				for (const TSharedPtr<FJsonValue>& G : *Graphs)
				{
					const TSharedPtr<FJsonObject> GO = G->AsObject();
					if (!GO.IsValid())
					{
						continue;
					}
					FGraph Graph;
					GO->TryGetStringField(TEXT("id"), Graph.Id);  // null -> stays empty = main
					Graph.Name = GO->GetStringField(TEXT("name"));
					Project.Graphs.Add(Graph);
				}
			}
			Self->ProjectOptions.Add(MakeShared<FString>(Project.Name));
			Self->Projects.Add(MoveTemp(Project));
		}
		Self->ProjectCombo->RefreshOptions();
		if (Self->ProjectOptions.Num() > 0)
		{
			Self->ProjectCombo->SetSelectedItem(Self->ProjectOptions[0]);
			Self->SetStatus(TEXT(""));
		}
		else
		{
			Self->SetStatus(TEXT("The orchestrator has no projects yet"));
		}
	});
	Req->ProcessRequest();
}

void SOrchestratorSendDialog::OnProjectChanged(TSharedPtr<FString> Item, ESelectInfo::Type)
{
	GraphOptions.Reset();
	const int32 Index = ProjectOptions.IndexOfByKey(Item);
	if (Projects.IsValidIndex(Index))
	{
		for (const FGraph& G : Projects[Index].Graphs)
		{
			GraphOptions.Add(MakeShared<FString>(G.Name));
		}
	}
	GraphCombo->RefreshOptions();
	if (GraphOptions.Num() > 0)
	{
		GraphCombo->SetSelectedItem(GraphOptions[0]);
	}
}

FReply SOrchestratorSendDialog::OnCancel()
{
	if (TSharedPtr<SWindow> Win = Window.Pin())
	{
		Win->RequestDestroyWindow();
	}
	return FReply::Handled();
}

FReply SOrchestratorSendDialog::OnSend()
{
	const int32 P = ProjectOptions.IndexOfByKey(ProjectCombo->GetSelectedItem());
	if (!Projects.IsValidIndex(P))
	{
		SetStatus(TEXT("Pick a project"));
		return FReply::Handled();
	}
	const int32 G = GraphOptions.IndexOfByKey(GraphCombo->GetSelectedItem());
	SendProjectId = Projects[P].Id;
	SendGraphId = Projects[P].Graphs.IsValidIndex(G) ? Projects[P].Graphs[G].Id : FString();
	NextIndex = 0;
	Sent = 0;
	Errors.Reset();
	bBusy = true;
	SendNext();
	return FReply::Handled();
}

/** One export attempt; false (with a reason) if the exporter is missing or wrote nothing. */
/** glTF exporter options, set explicitly: left to the defaults it exported a Nanite mesh's render
 *  data (the ~3k-triangle fallback of a 14M-triangle body) and baked no Base Color at all
 *  (baseColorFactor 0 = black, only occlusion baked) -- 2026-11-22.
 *  The source model is asked for STATIC meshes only: that is where Nanite hides the real
 *  geometry, while a skeletal mesh's render LOD0 is already full detail and is the data that
 *  carries the skin -- with the source model too, skeletal meshes arrived as static ones. */
static UObject* MakeGltfOptions(UObject* Object, bool bSourceGeometry)
{
	UGLTFExportOptions* Options = NewObject<UGLTFExportOptions>();
	Options->ResetToDefault();
	Options->bExportSourceModel = bSourceGeometry && Object->IsA<UStaticMesh>();
	Options->BakeMaterialInputs = EGLTFMaterialBakeMode::Simple;  // bake complex inputs into textures
	Options->DefaultMaterialBakeSize.X = 2048;
	Options->DefaultMaterialBakeSize.Y = 2048;
	Options->DefaultMaterialBakeSize.bAutoDetect = false;
	// a rig, its clips and shape keys
	Options->bExportVertexSkinWeights = true;
	Options->bExportAnimationSequences = true;
	Options->bExportMorphTargets = true;
	return Options;
}

static bool TryExport(UObject* Object, const FString& Ext, const FString& OutPath, bool bSourceGeometry, FString& OutError)
{
	UExporter* Exporter = UExporter::FindExporter(Object, *Ext);
	if (!Exporter)
	{
		OutError = FString::Printf(TEXT("no .%s exporter for %s"), *Ext, *Object->GetClass()->GetName());
		return false;
	}
	IFileManager::Get().Delete(*OutPath, false, true, true);
	UAssetExportTask* Task = NewObject<UAssetExportTask>();
	Task->AddToRoot();
	Task->Object = Object;
	Task->Exporter = Exporter;
	Task->Filename = OutPath;
	Task->bSelected = false;
	Task->bReplaceIdentical = true;
	Task->bPrompt = false;
	Task->bAutomated = true;
	if (Ext == TEXT("glb"))
	{
		Task->Options = MakeGltfOptions(Object, bSourceGeometry);
	}
	else if (Ext == TEXT("fbx") && Object->IsA<UAnimSequence>())
	{
		// An animation is wanted as just that: skeleton + clip, no preview mesh (the person's
		// call; the orchestrator draws a mesh-less FBX as a stick skeleton that plays the clip).
		UFbxExportOption* Fbx = NewObject<UFbxExportOption>();
		Fbx->bExportPreviewMesh = false;
		Task->Options = Fbx;
	}
	const bool bOk = UExporter::RunAssetExportTask(Task);
	const TArray<FString> TaskErrors = Task->Errors;
	Task->RemoveFromRoot();
	if (!bOk || !FPaths::FileExists(OutPath) || IFileManager::Get().FileSize(*OutPath) <= 0)
	{
		OutError = FString::Printf(TEXT(".%s export failed%s%s"), *Ext,
			TaskErrors.Num() ? TEXT(": ") : TEXT(" (see Output Log)"), TaskErrors.Num() ? *TaskErrors[0] : TEXT(""));
		return false;
	}
	return true;
}

bool SOrchestratorSendDialog::ExportOne(UObject* Object, FString& OutPath, FString& OutMime, FString& OutError) const
{
	// Meshes: GLB first -- the orchestrator shows it natively and the glTF exporter carries baked
	// materials -- and FBX when the GLB fails. Animations: FBX, skeleton + clip only.
	TArray<TPair<FString, FString>> Formats;
	if (Object->IsA<UTexture>())
	{
		Formats.Add({TEXT("png"), TEXT("image/png")});
	}
	else if (Object->IsA<UAnimSequence>())
	{
		Formats.Add({TEXT("fbx"), TEXT("model/fbx")});
	}
	else if (Object->IsA<UStaticMesh>() || Object->IsA<USkeletalMesh>())
	{
		Formats.Add({TEXT("glb"), TEXT("model/gltf-binary")});
		Formats.Add({TEXT("fbx"), TEXT("model/fbx")});
	}
	else
	{
		OutError = FString::Printf(TEXT("%s: a %s can't be sent (meshes, animations and textures can)"),
			*Object->GetName(), *Object->GetClass()->GetName());
		return false;
	}
	// Exporters (the FBX SDK above all) don't create the folder they write into.
	const FString Dir = FPaths::ConvertRelativePathToFull(FPaths::ProjectSavedDir() / TEXT("Orchestrator") / TEXT("Outgoing"));
	IFileManager::Get().MakeDirectory(*Dir, true);

	TArray<FString> Tried;
	for (const TPair<FString, FString>& Format : Formats)
	{
		OutPath = Dir / (OrchestratorUtils::SafeName(Object->GetName(), TEXT("Asset")) + TEXT(".") + Format.Key);
		FString Why;
		if (TryExport(Object, Format.Key, OutPath, bSourceGeometry, Why))
		{
			OutMime = Format.Value;
			return true;
		}
		Tried.Add(Why);
	}
	OutError = Object->GetName() + TEXT(": ") + FString::Join(Tried, TEXT("; "));
	return false;
}

static TArray<UMaterialInterface*> MeshMaterials(UObject* Object)
{
	TArray<UMaterialInterface*> Mats;
	if (const UStaticMesh* SM = Cast<UStaticMesh>(Object))
	{
		for (const FStaticMaterial& M : SM->GetStaticMaterials())
		{
			Mats.Add(M.MaterialInterface);
		}
	}
	else if (const USkeletalMesh* SK = Cast<USkeletalMesh>(Object))
	{
		for (const FSkeletalMaterial& M : SK->GetMaterials())
		{
			Mats.Add(M.MaterialInterface);
		}
	}
	return Mats;
}

/** Unreal's glTF exporter can't read a Substrate material graph: a mesh on the Interchange
 *  glTF material (M_GLTF, r.Substrate=1) came out black (Base Color 0), though its instance
 *  holds plain glTF-named texture parameters. Those textures go along as PNG parts
 *  (tex_N) with a map of which material slot each fills; the server writes them into the
 *  GLB's materials (core/glb_patch.py), only where the exporter left the slot empty. */
static FString CollectMaterialTextures(UObject* Object, TArray<OrchestratorUtils::FMultipartFile>& Files)
{
	// glTF slot -> the texture parameter names that may fill it, first match wins
	struct FSlot
	{
		const TCHAR* Slot;
		const TCHAR* Params[5];
	};
	static const FSlot Slots[] = {
		{TEXT("baseColor"), {TEXT("BaseColorTexture"), TEXT("BaseColor"), TEXT("Albedo"), TEXT("DiffuseTexture"), TEXT("Diffuse")}},
		{TEXT("metallicRoughness"), {TEXT("MetallicRoughnessTexture"), nullptr, nullptr, nullptr, nullptr}},
		{TEXT("normal"), {TEXT("NormalTexture"), TEXT("Normal"), nullptr, nullptr, nullptr}},
		{TEXT("emissive"), {TEXT("EmissiveTexture"), nullptr, nullptr, nullptr, nullptr}},
	};
	const FString Dir = FPaths::ConvertRelativePathToFull(FPaths::ProjectSavedDir() / TEXT("Orchestrator") / TEXT("Outgoing"));
	IFileManager::Get().MakeDirectory(*Dir, true);
	TSharedRef<FJsonObject> Fixups = MakeShared<FJsonObject>();
	TMap<FString, FString> KeyOfTexture;  // one texture used by several materials goes once
	for (UMaterialInterface* Mi : MeshMaterials(Object))
	{
		if (!Mi)
		{
			continue;
		}
		TSharedRef<FJsonObject> Fix = MakeShared<FJsonObject>();
		for (const FSlot& Slot : Slots)
		{
			for (const TCHAR* ParamName : Slot.Params)
			{
				if (!ParamName)
				{
					break;
				}
				UTexture* Tex = nullptr;
				if (!Mi->GetTextureParameterValue(FMaterialParameterInfo(FName(ParamName)), Tex) || !Tex)
				{
					continue;
				}
				const FString TexPath = Tex->GetPathName();
				// the Interchange material's 1x1 white/flat placeholders mean "not set"
				if (TexPath.StartsWith(TEXT("/InterchangeAssets/")) || TexPath.StartsWith(TEXT("/Engine/")))
				{
					continue;
				}
				FString* Key = KeyOfTexture.Find(TexPath);
				if (!Key)
				{
					const FString NewKey = FString::Printf(TEXT("tex_%d"), KeyOfTexture.Num());
					const FString PngPath = Dir / (OrchestratorUtils::SafeName(Tex->GetName(), NewKey) + TEXT(".png"));
					FString Why;
					if (!TryExport(Tex, TEXT("png"), PngPath, false, Why))
					{
						UE_LOG(LogTemp, Warning, TEXT("[Orchestrator] %s: %s"), *TexPath, *Why);
						break;
					}
					OrchestratorUtils::FMultipartFile File;
					File.Field = NewKey;
					File.FileName = FPaths::GetCleanFilename(PngPath);
					File.Mime = TEXT("image/png");
					FFileHelper::LoadFileToArray(File.Data, *PngPath);
					IFileManager::Get().Delete(*PngPath, false, true, true);
					Files.Add(MoveTemp(File));
					Key = &KeyOfTexture.Add(TexPath, NewKey);
				}
				Fix->SetStringField(Slot.Slot, *Key);
				break;
			}
		}
		FLinearColor Factor;
		if (Mi->GetVectorParameterValue(FMaterialParameterInfo(TEXT("BaseColorFactor")), Factor))
		{
			TArray<TSharedPtr<FJsonValue>> Rgba;
			for (float V : {Factor.R, Factor.G, Factor.B, Factor.A})
			{
				Rgba.Add(MakeShared<FJsonValueNumber>(V));
			}
			Fix->SetArrayField(TEXT("baseColorFactor"), Rgba);
		}
		if (Fix->Values.Num() > 0)
		{
			Fixups->SetObjectField(Mi->GetName(), Fix);
		}
	}
	return Fixups->Values.Num() > 0 ? OrchestratorUtils::ToJson(Fixups) : FString();
}

/** What the orchestrator needs to tell why a mesh's colour didn't survive the glTF export,
 *  sent with the upload (meta.bridge_diag) and logged: per material its chain, shading model,
 *  Material Attributes / Substrate wiring, and the texture + colour parameters it sets. */
static FString DescribeMaterials(UObject* Object)
{
	const TArray<UMaterialInterface*> Mats = MeshMaterials(Object);
	if (Mats.Num() == 0)
	{
		return FString();
	}
	FString Out;
	const IConsoleVariable* SubstrateVar = IConsoleManager::Get().FindConsoleVariable(TEXT("r.Substrate"));
	Out += FString::Printf(TEXT("UE %s, r.Substrate=%d\n"), *FEngineVersion::Current().ToString(),
		SubstrateVar ? SubstrateVar->GetInt() : -1);
	for (UMaterialInterface* Mi : Mats)
	{
		if (!Mi)
		{
			Out += TEXT("- (empty slot)\n");
			continue;
		}
		FString Chain = Mi->GetPathName();
		for (const UMaterialInstance* Inst = Cast<UMaterialInstance>(Mi); Inst && Inst->Parent; Inst = Cast<UMaterialInstance>(Inst->Parent))
		{
			Chain += TEXT(" <- ") + Inst->Parent->GetPathName();
		}
		Out += TEXT("- ") + Chain + TEXT("\n");
		if (const UMaterial* Base = Mi->GetMaterial())
		{
			const FMaterialShadingModelField Models = Base->GetShadingModels();
			const UEnum* ModelEnum = StaticEnum<EMaterialShadingModel>();
			FString ModelNames;
			for (int32 i = 0; i < (int32)MSM_NUM; ++i)
			{
				if (Models.HasShadingModel((EMaterialShadingModel)i))
				{
					ModelNames += (ModelNames.IsEmpty() ? TEXT("") : TEXT(",")) + ModelEnum->GetNameStringByValue(i);
				}
			}
			const UMaterialEditorOnlyData* Ed = Base->GetEditorOnlyData();
			Out += FString::Printf(TEXT("  shading=%s materialAttributes=%d baseColorLinked=%d frontMaterialLinked=%d domain=%d blend=%d\n"),
				*ModelNames, Base->bUseMaterialAttributes ? 1 : 0,
				Ed && Ed->BaseColor.IsConnected() ? 1 : 0,
				Ed && Ed->FrontMaterial.IsConnected() ? 1 : 0,
				(int32)Base->MaterialDomain, (int32)Base->BlendMode);
		}
		TArray<FMaterialParameterInfo> Infos;
		TArray<FGuid> Ids;
		Mi->GetAllTextureParameterInfo(Infos, Ids);
		for (const FMaterialParameterInfo& Info : Infos)
		{
			UTexture* Tex = nullptr;
			if (Mi->GetTextureParameterValue(Info, Tex) && Tex)
			{
				Out += FString::Printf(TEXT("  tex %s = %s (sRGB=%d)\n"), *Info.Name.ToString(), *Tex->GetPathName(), Tex->SRGB ? 1 : 0);
			}
		}
		Infos.Reset();
		Ids.Reset();
		Mi->GetAllVectorParameterInfo(Infos, Ids);
		for (const FMaterialParameterInfo& Info : Infos)
		{
			FLinearColor C;
			if (Mi->GetVectorParameterValue(Info, C))
			{
				Out += FString::Printf(TEXT("  vec %s = (%.3f, %.3f, %.3f, %.3f)\n"), *Info.Name.ToString(), C.R, C.G, C.B, C.A);
			}
		}
	}
	return Out.Left(20000);
}

void SOrchestratorSendDialog::SendNext()
{
	if (NextIndex >= Assets.Num())
	{
		bBusy = false;
		FString Done = FString::Printf(TEXT("Sent %d of %d"), Sent, Assets.Num());
		if (Errors.Num())
		{
			Done += TEXT("\n") + FString::Join(Errors, TEXT("\n"));
		}
		SetStatus(Done);
		FOrchestratorConnection::Notify(TEXT("Orchestrator: ") + Done.Replace(TEXT("\n"), TEXT("; ")), Errors.Num() > 0);
		if (Errors.Num() == 0)
		{
			OnCancel();
		}
		return;
	}
	const FAssetData Data = Assets[NextIndex++];
	SetStatus(FString::Printf(TEXT("Sending %s (%d/%d)..."), *Data.AssetName.ToString(), NextIndex, Assets.Num()));

	UObject* Object = Data.GetAsset();
	FString Path, Mime, Error;
	if (!Object || !ExportOne(Object, Path, Mime, Error))
	{
		Errors.Add(Object ? Error : Data.AssetName.ToString() + TEXT(": couldn't load"));
		SendNext();
		return;
	}
	TArray<uint8> FileData;
	FFileHelper::LoadFileToArray(FileData, *Path);
	IFileManager::Get().Delete(*Path, false, true, true);

	TMap<FString, FString> Fields;
	Fields.Add(TEXT("project_id"), SendProjectId);
	Fields.Add(TEXT("dashboard_id"), SendGraphId);
	Fields.Add(TEXT("place"), bPlace ? TEXT("true") : TEXT("false"));
	Fields.Add(TEXT("source_path"), Data.GetObjectPathString());
	const FString Diag = DescribeMaterials(Object);
	if (!Diag.IsEmpty())
	{
		UE_LOG(LogTemp, Log, TEXT("[Orchestrator] materials of %s:\n%s"), *Data.AssetName.ToString(), *Diag);
		Fields.Add(TEXT("diagnostics"), Diag);
	}
	TArray<OrchestratorUtils::FMultipartFile> Files;
	Files.Add({TEXT("file"), FPaths::GetCleanFilename(Path), Mime, MoveTemp(FileData)});
	if (Mime == TEXT("model/gltf-binary"))
	{
		const FString Fixups = CollectMaterialTextures(Object, Files);
		if (!Fixups.IsEmpty())
		{
			Fields.Add(TEXT("material_fixups"), Fixups);
		}
	}
	TArray<uint8> Body;
	const FString ContentType = OrchestratorUtils::BuildMultipartFiles(Fields, Files, Body);

	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> Req = Connection->NewRequest(TEXT("POST"), TEXT("/api/bridge/device/upload"));
	Req->SetHeader(TEXT("Content-Type"), ContentType);
	Req->SetContent(MoveTemp(Body));
	Req->SetTimeout(900.f);
	TWeakPtr<SOrchestratorSendDialog> Weak = StaticCastSharedRef<SOrchestratorSendDialog>(AsShared());
	const FString Name = Data.AssetName.ToString();
	Req->OnProcessRequestComplete().BindLambda([Weak, Name](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		TSharedPtr<SOrchestratorSendDialog> Self = Weak.Pin();
		if (!Self.IsValid())
		{
			return;
		}
		if (bOk && Resp.IsValid() && Resp->GetResponseCode() == 201)
		{
			Self->Sent++;
		}
		else
		{
			FString Detail = Resp.IsValid() ? Resp->GetContentAsString().Left(300) : TEXT("no answer");
			Self->Errors.Add(FString::Printf(TEXT("%s: upload failed (%d) %s"), *Name, Resp.IsValid() ? Resp->GetResponseCode() : 0, *Detail));
		}
		Self->SendNext();
	});
	Req->ProcessRequest();
}

#undef LOCTEXT_NAMESPACE
