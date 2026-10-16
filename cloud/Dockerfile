FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY cloud/MoodPress.CloudPortal.csproj cloud/
RUN dotnet restore cloud/MoodPress.CloudPortal.csproj
COPY cloud/ cloud/
RUN dotnet publish cloud/MoodPress.CloudPortal.csproj --configuration Release --no-restore --output /app/publish

FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS runtime
WORKDIR /app
ENV ASPNETCORE_URLS=http://+:8080
ENV ASPNETCORE_ENVIRONMENT=Production
ENV MOODPRESS_DATABASE_PATH=/data/moodpress-cloud.db
COPY --from=build /app/publish/ .
USER root
RUN mkdir -p /data && chown -R 1654:1654 /data
USER 1654:1654
EXPOSE 8080
ENTRYPOINT ["dotnet", "MoodPress.CloudPortal.dll"]
